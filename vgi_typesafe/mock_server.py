"""A mocked TypeSafe endpoint: ``POST /v1/systemone``, choice questions only.

It speaks the real wire format — Bearer auth, the ``state``/``model``/``questions``
request, the ``answers``/``usage`` response, 401 and 422 errors — so the worker
cannot tell it from production. What it does *not* have is a model: options are
scored by keyword overlap between the state and each option's name and
description, then softmaxed. That is deterministic, which is the point — tests
can assert exact choices.

    uv run vgi-typesafe-mock --port 8787 --api-key test-key

    CREATE SECRET (TYPE typesafe, api_key 'test-key', base_url 'http://127.0.0.1:8787');

Stdlib only, so it adds nothing to the worker's dependencies.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SYSTEM_ONE_PATH = "/v1/systemone"
MAX_OPTIONS = 255

#: How sharply one extra keyword hit separates two options.
_SHARPNESS = 2.0

_STOPWORDS = frozenset(
    {
        *("the", "and", "for", "with", "that", "this", "from", "have", "has", "had", "was", "were"),
        *("are", "not", "but", "you", "your", "our", "its", "can", "will", "would", "could", "should"),
        *("about", "into", "over", "when", "what", "which", "who", "how", "why", "any", "all"),
    }
)


class RequestInvalid(ValueError):
    """The request body fails validation; becomes a 422."""


def _stem(word: str) -> str:
    """Crude suffix folding, so "charged" meets "charges" and "package" meets "packages"."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    return word[:-1] if word.endswith("e") and len(word) > 3 else word


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [_stem(w) for w in words if len(w) >= 3 and w not in _STOPWORDS]


def _flatten(value: Any) -> str:
    """All the text in a JSON value — a state may be a string, object, or array."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(f"{k} {_flatten(v)}" for k, v in value.items())
    if isinstance(value, list):
        return " ".join(_flatten(v) for v in value)
    return "" if value is None else str(value)


def _option_vocab(name: str, description: Any) -> tuple[set[str], set[str]]:
    """``(attracting, repelling)`` token sets for one option.

    A structured description's ``not_for`` text counts *against* the option,
    mirroring what the field means to the real model.
    """
    if isinstance(description, dict):
        positive = _flatten([description.get("what"), description.get("examples")])
        negative = _flatten(description.get("not_for"))
    else:
        positive, negative = _flatten(description), ""
    return set(_tokens(f"{name} {positive}")), set(_tokens(negative))


def answer_choice(state: Any, question: dict[str, Any]) -> dict[str, Any]:
    """Score one choice question against the state."""
    criteria = question["criteria"]
    state_tokens = _tokens(_flatten(state))
    scores: dict[str, float] = {}
    for name, description in criteria.items():
        attract, repel = _option_vocab(name, description)
        scores[name] = float(sum((t in attract) - (t in repel) for t in state_tokens))

    peak = max(scores.values())
    weights = {name: math.exp(_SHARPNESS * (score - peak)) for name, score in scores.items()}
    total = sum(weights.values())
    probabilities = {name: round(weight / total, 6) for name, weight in weights.items()}

    # Confidence as probability concentration: 1 minus normalized entropy.
    entropy = -sum(p * math.log(p) for p in probabilities.values() if p > 0)
    confidence = 1.0 if len(criteria) == 1 else max(0.0, 1.0 - entropy / math.log(len(criteria)))

    # Ties go to the first-listed option, so results do not depend on dict hashing.
    choice = max(probabilities, key=lambda name: probabilities[name])
    return {
        "type": "choice",
        "choice": choice,
        "confidence": round(confidence, 6),
        "probabilities": probabilities,
    }


def _validate(body: Any) -> tuple[Any, str, dict[str, dict[str, Any]]]:
    if not isinstance(body, dict):
        raise RequestInvalid("request body must be a JSON object")
    if "state" not in body or body["state"] is None:
        raise RequestInvalid("'state' is required")
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise RequestInvalid("'model' is required")
    questions = body.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise RequestInvalid("'questions' must be a non-empty object")
    for qid, question in questions.items():
        if not isinstance(question, dict):
            raise RequestInvalid(f"question {qid!r} must be an object")
        if question.get("type") != "choice":
            raise RequestInvalid(
                f"question {qid!r}: this mock only answers type 'choice', got {question.get('type')!r}"
            )
        if not isinstance(question.get("instructions"), str) or not question["instructions"].strip():
            raise RequestInvalid(f"question {qid!r}: 'instructions' is required")
        criteria = question.get("criteria")
        if not isinstance(criteria, dict) or not criteria:
            raise RequestInvalid(f"question {qid!r}: 'criteria' must be a non-empty object")
        if len(criteria) > MAX_OPTIONS:
            raise RequestInvalid(f"question {qid!r}: at most {MAX_OPTIONS} options are allowed")
    return body["state"], model, questions


def handle(body: Any) -> dict[str, Any]:
    """Answer one decoded System One request. Raises :class:`RequestInvalid`."""
    state, model, questions = _validate(body)
    answers = {qid: answer_choice(state, question) for qid, question in questions.items()}
    return {
        "model": model,
        "answers": answers,
        "usage": {
            "input_tokens": len(_flatten(body).split()),
            "output_tokens": len(answers),
        },
    }


class MockTypeSafeServer(ThreadingHTTPServer):
    """The mock endpoint. ``api_key=None`` accepts any non-empty Bearer token."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], api_key: str | None = None) -> None:
        super().__init__(address, _Handler)
        self.api_key = api_key
        #: Every accepted request body, for tests to assert on.
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def record(self, body: dict[str, Any]) -> None:
        with self._lock:
            self.requests.append(body)


class _Handler(BaseHTTPRequestHandler):
    server: MockTypeSafeServer

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802 - http.server's naming
        # Drain the body before any early reply, or a keep-alive client sees
        # its unread bytes parsed as the next request.
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))

        if self.path.rstrip("/") != SYSTEM_ONE_PATH:
            self._send(404, {"detail": f"no such endpoint: {self.path}"})
            return

        scheme, _, token = (self.headers.get("Authorization") or "").partition(" ")
        expected = self.server.api_key
        if scheme.lower() != "bearer" or not token.strip() or (expected and token.strip() != expected):
            self._send(401, {"detail": "invalid or missing API key"})
            return

        try:
            body = json.loads(raw)
            result = handle(body)
        except json.JSONDecodeError:
            self._send(422, {"detail": "request body is not valid JSON"})
            return
        except RequestInvalid as exc:
            self._send(422, {"detail": str(exc)})
            return
        self.server.record(body)
        self._send(200, result)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server's signature
        """Silence per-request stderr logging; tests read ``server.requests`` instead."""


@contextmanager
def running(api_key: str | None = None, port: int = 0) -> Iterator[MockTypeSafeServer]:
    """Run the mock on a background thread for the duration of the block."""
    server = MockTypeSafeServer(("127.0.0.1", port), api_key=api_key)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> None:
    """Run the mock endpoint in the foreground."""
    parser = argparse.ArgumentParser(description="Mock TypeSafe /v1/systemone endpoint (choice questions)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--api-key", default=None, help="require this exact key (default: accept any)")
    args = parser.parse_args()
    server = MockTypeSafeServer((args.host, args.port), api_key=args.api_key)
    print(f"mock TypeSafe endpoint listening on {server.base_url}{SYSTEM_ONE_PATH}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
