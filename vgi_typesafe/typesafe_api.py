"""The one place this worker talks HTTP: ``POST /v1/systemone``.

A System One request carries a single ``state`` and a map of named questions,
all answered in parallel by the API. So however many questions a row asks, it
costs one request — and a batch of input rows is one request per *distinct*
state, issued concurrently.

Three question types exist: ``choice`` (pick one option), ``noul`` (a yes/no
probability) and ``score`` (a position on an ordered scale). :func:`ask_many` is
the general path; :func:`ask_choices` is the one-question shorthand ``choice()``
uses, and rides on it.

Failure policy: 429 (rate limited) and 529 (overloaded) are retried with
exponential backoff, as the API reference asks. Everything else raises
:class:`TypeSafeError` and surfaces as a DuckDB error — a judgment that silently
became NULL would be indistinguishable from a NULL input.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx

from vgi_typesafe import __version__
from vgi_typesafe.auth import Credentials

SYSTEM_ONE_PATH = "/v1/systemone"
DEFAULT_MODEL = "jev-latest"
#: Matches the official SDK's per-operation default.
DEFAULT_TIMEOUT = 10.0

QUESTION_TYPES = ("choice", "noul", "score")

#: The API caps a choice question at 255 options.
MAX_OPTIONS = 255

#: A score question's criteria is an ordered scale of 2-10 levels.
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10

#: The name ``choice()`` gives its single question in each request.
QUESTION_ID = "choice"

RETRY_STATUSES = frozenset({429, 529})
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 8.0


class TypeSafeError(RuntimeError):
    """The API answered with an error, or with something that is not an answer."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(slots=True, frozen=True)
class Response:
    """One answered request: every question's parsed answer, plus usage.

    ``answers[question_id]`` is a plain dict shaped by the question's type:

    * choice — ``{"choice": str, "confidence": float, "probabilities": {option: float}}``
    * noul   — ``{"noul": float}``
    * score  — ``{"score": float, "confidence": float, "probabilities": {level: float}}``
    """

    answers: dict[str, dict[str, Any]]
    model: str
    input_tokens: int | None
    output_tokens: int | None


@dataclass(slots=True, frozen=True)
class ChoiceAnswer:
    """One answered choice question — the shape ``choice()`` consumes."""

    choice: str
    confidence: float
    probabilities: dict[str, float]
    model: str
    input_tokens: int | None
    output_tokens: int | None


def open_client(timeout: float = DEFAULT_TIMEOUT) -> httpx.Client:
    """A client for one batch of requests. Safe to share across threads."""
    return httpx.Client(timeout=timeout, headers={"User-Agent": f"vgi-typesafe/{__version__}"})


def _error_detail(response: httpx.Response) -> str:
    """The API's own explanation, if it sent one."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        for key in ("detail", "error", "message"):
            if body.get(key):
                return str(body[key])[:300]
    return str(body)[:300]


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Honour ``Retry-After`` when the server sends a usable one, else back off."""
    header = response.headers.get("retry-after", "")
    try:
        return min(max(float(header), 0.0), BACKOFF_CAP_SECONDS)
    except ValueError:
        return min(BACKOFF_BASE_SECONDS * 2**attempt, BACKOFF_CAP_SECONDS)


def _post(client: httpx.Client, credentials: Credentials, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{credentials.base_url}{SYSTEM_ONE_PATH}"
    headers = {"Authorization": f"Bearer {credentials.api_key}"}
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise TypeSafeError(f"could not reach TypeSafe at {url}: {exc}") from exc
        if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
            time.sleep(_retry_delay(response, attempt))
            continue
        if response.status_code == 401:
            raise TypeSafeError(
                f"TypeSafe rejected the API key (HTTP 401): {_error_detail(response)}", status=401
            )
        if response.status_code >= 400:
            raise TypeSafeError(
                f"TypeSafe returned HTTP {response.status_code}: {_error_detail(response)}",
                status=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise TypeSafeError("TypeSafe returned a non-JSON response") from exc
        if not isinstance(body, dict):
            raise TypeSafeError("TypeSafe returned a JSON response that is not an object")
        return body
    raise AssertionError("unreachable")  # pragma: no cover - the loop always returns or raises


def _number(answer: dict[str, Any], key: str, question_id: str) -> float:
    try:
        return float(answer[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise TypeSafeError(
            f"TypeSafe answer {question_id!r} has no numeric {key!r}: {str(answer)[:300]}"
        ) from exc


def _parse_answer(question_id: str, question: Mapping[str, Any], answer: Any) -> dict[str, Any]:
    """Validate one answer against the question that was asked, and normalise it.

    Probabilities are reported in the *caller's* order — options as the criteria
    listed them, score levels ascending — so the MAP reads the same on every row.
    """
    kind = question["type"]
    if not isinstance(answer, dict):
        raise TypeSafeError(f"TypeSafe response has no {question_id!r} answer")
    if answer.get("type", kind) != kind:
        raise TypeSafeError(
            f"TypeSafe answered {question_id!r} as {answer.get('type')!r}, but it was asked as {kind!r}"
        )
    raw = answer.get("probabilities") or {}
    if kind == "noul":
        return {"noul": _number(answer, "noul", question_id)}
    if kind == "choice":
        if "choice" not in answer:
            raise TypeSafeError(f"TypeSafe answer {question_id!r} has no 'choice': {str(answer)[:300]}")
        return {
            "choice": str(answer["choice"]),
            "confidence": float(answer.get("confidence", 0.0)),
            "probabilities": {o: float(raw[o]) for o in question["criteria"] if o in raw},
        }
    levels = range(len(question["criteria"]))
    return {
        "score": _number(answer, "score", question_id),
        "confidence": float(answer.get("confidence", 0.0)),
        # The API keys levels as strings ("0", "1", ...); SQL wants them as integers.
        "probabilities": {level: float(raw[str(level)]) for level in levels if str(level) in raw},
    }


def ask(
    state: Any,
    questions: Mapping[str, Mapping[str, Any]],
    *,
    credentials: Credentials,
    client: httpx.Client,
    model: str = DEFAULT_MODEL,
) -> Response:
    """Ask every question about one state — a single request."""
    payload = {"state": state, "model": model, "questions": {k: dict(v) for k, v in questions.items()}}
    body = _post(client, credentials, payload)
    raw_answers = body.get("answers")
    if not isinstance(raw_answers, dict):
        raise TypeSafeError(f"TypeSafe response has no 'answers': {str(body)[:300]}")
    usage = body.get("usage") or {}
    return Response(
        answers={qid: _parse_answer(qid, q, raw_answers.get(qid)) for qid, q in questions.items()},
        model=str(body.get("model") or ""),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
    )


def _state_key(state: Any) -> str:
    """A hashable identity for a state, which may be an (unhashable) object or array."""
    return json.dumps(state, sort_keys=True, ensure_ascii=False)


def ask_many(
    states: Sequence[Any],
    questions: Mapping[str, Mapping[str, Any]],
    *,
    credentials: Credentials,
    client: httpx.Client,
    model: str = DEFAULT_MODEL,
    concurrency: int = 8,
) -> list[Response | None]:
    """Ask the same questions of every state, in input order.

    A ``None`` state yields ``None`` without a request. Repeated states — common
    under a LATERAL over a low-cardinality column — are asked once.
    """
    distinct: dict[str, Any] = {}
    keys: list[str | None] = []
    for state in states:
        key = None if state is None else _state_key(state)
        keys.append(key)
        if key is not None:
            distinct.setdefault(key, state)
    if not distinct:
        return [None] * len(states)

    def one(state: Any) -> Response:
        return ask(state, questions, credentials=credentials, client=client, model=model)

    if len(distinct) == 1 or concurrency <= 1:
        answered = [one(s) for s in distinct.values()]
    else:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(distinct))) as pool:
            answered = list(pool.map(one, distinct.values()))
    by_key = dict(zip(distinct, answered, strict=True))
    return [None if key is None else by_key[key] for key in keys]


def ask_choices(
    states: Sequence[str | None],
    *,
    instructions: str,
    criteria: Mapping[str, str],
    credentials: Credentials,
    client: httpx.Client,
    model: str = DEFAULT_MODEL,
    concurrency: int = 8,
) -> list[ChoiceAnswer | None]:
    """The one-question shorthand: the same choice question for every state."""
    question = {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}
    responses = ask_many(
        states,
        {QUESTION_ID: question},
        credentials=credentials,
        client=client,
        model=model,
        concurrency=concurrency,
    )
    # Identical states share one Response; share one ChoiceAnswer too.
    converted: dict[int, ChoiceAnswer] = {}
    out: list[ChoiceAnswer | None] = []
    for response in responses:
        if response is None:
            out.append(None)
            continue
        if id(response) not in converted:
            converted[id(response)] = ChoiceAnswer(
                **response.answers[QUESTION_ID],
                model=response.model,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
        out.append(converted[id(response)])
    return out
