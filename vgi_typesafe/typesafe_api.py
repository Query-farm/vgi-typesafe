"""The one place this worker talks HTTP: ``POST /v1/systemone``.

A System One request carries a single ``state`` and a map of named questions.
This worker asks one ``choice`` question per state, so a batch of input rows is
one request per *distinct* state, issued concurrently.

Failure policy: 429 (rate limited) and 529 (overloaded) are retried with
exponential backoff, as the API reference asks. Everything else raises
:class:`TypeSafeError` and surfaces as a DuckDB error — a classification that
silently became NULL would be indistinguishable from a NULL input.
"""

from __future__ import annotations

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

#: The API caps a choice question at 255 options.
MAX_OPTIONS = 255

#: The name this worker gives its single question in each request.
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
class ChoiceAnswer:
    """One answered choice question."""

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


def _parse_choice(body: dict[str, Any], options: Sequence[str]) -> ChoiceAnswer:
    answer = (body.get("answers") or {}).get(QUESTION_ID)
    if not isinstance(answer, dict) or "choice" not in answer:
        raise TypeSafeError(f"TypeSafe response has no {QUESTION_ID!r} answer: {str(body)[:300]}")
    raw_probabilities = answer.get("probabilities") or {}
    # Reported in the caller's option order, so the MAP reads the same on every row.
    probabilities = {o: float(raw_probabilities[o]) for o in options if o in raw_probabilities}
    usage = body.get("usage") or {}
    return ChoiceAnswer(
        choice=str(answer["choice"]),
        confidence=float(answer.get("confidence", 0.0)),
        probabilities=probabilities,
        model=str(body.get("model") or ""),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
    )


def ask_choice(
    state: Any,
    *,
    instructions: str,
    criteria: Mapping[str, str],
    credentials: Credentials,
    client: httpx.Client,
    model: str = DEFAULT_MODEL,
) -> ChoiceAnswer:
    """Ask one choice question about one state."""
    payload = {
        "state": state,
        "model": model,
        "questions": {
            QUESTION_ID: {"type": "choice", "instructions": instructions, "criteria": dict(criteria)},
        },
    }
    return _parse_choice(_post(client, credentials, payload), list(criteria))


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
    """Answer the same choice question for every state, in input order.

    A ``None`` state yields ``None`` without a request. Repeated states — common
    under a LATERAL over a low-cardinality column — are asked once.
    """
    distinct = list(dict.fromkeys(s for s in states if s is not None))
    if not distinct:
        return [None] * len(states)

    def ask(state: str) -> ChoiceAnswer:
        return ask_choice(
            state,
            instructions=instructions,
            criteria=criteria,
            credentials=credentials,
            client=client,
            model=model,
        )

    if len(distinct) == 1 or concurrency <= 1:
        answered = [ask(s) for s in distinct]
    else:
        with ThreadPoolExecutor(max_workers=min(concurrency, len(distinct))) as pool:
            answered = list(pool.map(ask, distinct))
    by_state = dict(zip(distinct, answered, strict=True))
    return [None if s is None else by_state[s] for s in states]
