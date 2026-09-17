# Copyright 2026 Query Farm LLC - https://query.farm

"""The HTTP layer: wire format, retries, errors, and per-batch de-duplication."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from vgi_typesafe import typesafe_api as api
from vgi_typesafe.auth import Credentials
from vgi_typesafe.mock_server import handle

CREDENTIALS = Credentials(api_key="secret-key", base_url="http://typesafe.test")
CRITERIA = {"shipping": "Delivery status, lost packages", "billing": "Charges, invoices"}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _mock_backend(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=handle(json.loads(request.content)))


def _ask(client: httpx.Client, states: list[str | None], **kwargs: Any) -> list[api.ChoiceAnswer | None]:
    return api.ask_choices(
        states,
        instructions="Which team?",
        criteria=CRITERIA,
        credentials=CREDENTIALS,
        client=client,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(api.time, "sleep", slept.append)
    return slept


class TestWireFormat:
    def test_request_matches_the_api_reference(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _mock_backend(request)

        _ask(_client(handler), ["lost package"], model="jev-1.13")

        (request,) = seen
        assert request.method == "POST"
        assert str(request.url) == "http://typesafe.test/v1/systemone"
        assert request.headers["authorization"] == "Bearer secret-key"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "state": "lost package",
            "model": "jev-1.13",
            "questions": {"choice": {"type": "choice", "instructions": "Which team?", "criteria": CRITERIA}},
        }

    def test_answer_is_parsed(self) -> None:
        (answer,) = _ask(_client(_mock_backend), ["I was charged twice on my invoice"])
        assert answer is not None
        assert answer.choice == "billing"
        assert 0.0 < answer.confidence <= 1.0
        assert answer.model == "jev-latest"
        assert answer.input_tokens and answer.output_tokens == 1

    def test_probabilities_follow_the_callers_option_order(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            answer = {"type": "choice", "choice": "billing", "confidence": 1.0}
            answer["probabilities"] = {"billing": 1.0, "shipping": 0.0}  # reversed on purpose
            return httpx.Response(200, json={"model": "m", "answers": {"choice": answer}})

        (answer,) = _ask(_client(handler), ["x"])
        assert answer is not None and list(answer.probabilities) == ["shipping", "billing"]


class TestBatching:
    def test_results_are_in_input_order(self) -> None:
        states = ["lost package", "invoice charges", "lost package delivery"]
        answers = _ask(_client(_mock_backend), states)
        assert [a.choice for a in answers if a] == ["shipping", "billing", "shipping"]

    def test_null_states_make_no_request(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not have been called")

        assert _ask(_client(handler), [None, None]) == [None, None]

    def test_nulls_keep_their_position(self) -> None:
        answers = _ask(_client(_mock_backend), [None, "lost package", None])
        assert [a is None for a in answers] == [True, False, True]

    def test_repeated_states_are_asked_once(self) -> None:
        """A LATERAL over a low-cardinality column repeats states; each is billed once."""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content)["state"])
            return _mock_backend(request)

        answers = _ask(_client(handler), ["a lost package", "invoice", "a lost package", "invoice", None])
        assert sorted(calls) == ["a lost package", "invoice"]
        assert answers[0] is answers[2] and answers[1] is answers[3]

    @pytest.mark.parametrize("concurrency", [1, 4])
    def test_concurrency_does_not_change_the_result(self, concurrency: int) -> None:
        states = [f"lost package {i}" if i % 2 else f"invoice {i}" for i in range(20)]
        answers = _ask(_client(_mock_backend), states, concurrency=concurrency)
        assert [a.choice for a in answers if a] == ["shipping" if i % 2 else "billing" for i in range(20)]


class TestRetries:
    @pytest.mark.parametrize("status", [429, 529])
    def test_rate_limit_and_overload_are_retried(self, status: int, _no_sleep: list[float]) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(status) if len(attempts) < 3 else _mock_backend(request)

        (answer,) = _ask(_client(handler), ["lost package"])
        assert answer is not None and answer.choice == "shipping"
        assert len(attempts) == 3
        assert _no_sleep == [0.5, 1.0], "exponential backoff"

    def test_retry_after_is_honoured(self, _no_sleep: list[float]) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(429, headers={"Retry-After": "3"})
            return _mock_backend(request)

        _ask(_client(handler), ["x"])
        assert _no_sleep == [3.0]

    def test_retries_are_bounded(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(429, json={"detail": "slow down"})

        with pytest.raises(api.TypeSafeError, match="HTTP 429: slow down") as excinfo:
            _ask(_client(handler), ["x"])
        assert excinfo.value.status == 429
        assert len(attempts) == api.MAX_ATTEMPTS

    @pytest.mark.parametrize("status", [400, 401, 422, 500])
    def test_other_errors_are_not_retried(self, status: int) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(status, json={"detail": "nope"})

        with pytest.raises(api.TypeSafeError, match="nope"):
            _ask(_client(handler), ["x"])
        assert len(attempts) == 1


class TestErrors:
    def test_a_401_names_the_key_but_never_prints_it(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "invalid or missing API key"})

        with pytest.raises(api.TypeSafeError, match="rejected the API key") as excinfo:
            _ask(_client(handler), ["x"])
        assert "secret-key" not in str(excinfo.value)

    def test_a_connection_failure_is_wrapped(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(api.TypeSafeError, match="could not reach TypeSafe at http://typesafe.test"):
            _ask(_client(handler), ["x"])

    @pytest.mark.parametrize(
        "response",
        [
            httpx.Response(200, text="<html>"),
            httpx.Response(200, json=[1, 2]),
            httpx.Response(200, json={"answers": {}}),
            httpx.Response(200, json={"answers": {"choice": {"type": "choice"}}}),
        ],
    )
    def test_a_malformed_success_raises_rather_than_becoming_null(self, response: httpx.Response) -> None:
        with pytest.raises(api.TypeSafeError):
            _ask(_client(lambda request: response), ["x"])


QUESTIONS = {
    "dept": {"type": "choice", "instructions": "Which team?", "criteria": CRITERIA},
    "angry": {"type": "noul", "instructions": "Is the customer furious?"},
    "severity": {
        "type": "score",
        "instructions": "How bad?",
        "criteria": ["minor", "disruptive delay", "outage"],
    },
}


def _ask_many(client: httpx.Client, states: list[Any], **kwargs: Any) -> list[api.Response | None]:
    return api.ask_many(states, QUESTIONS, credentials=CREDENTIALS, client=client, **kwargs)


class TestAskMany:
    def test_all_questions_travel_in_one_request(self) -> None:
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return _mock_backend(request)

        _ask_many(_client(handler), [{"message": "lost package", "tier": "gold"}])

        (payload,) = seen
        assert payload["state"] == {"message": "lost package", "tier": "gold"}
        assert payload["questions"] == QUESTIONS

    def test_each_answer_is_shaped_by_its_question_type(self) -> None:
        (response,) = _ask_many(_client(_mock_backend), ["furious about this outage and lost package"])
        assert response is not None
        assert set(response.answers["dept"]) == {"choice", "confidence", "probabilities"}
        assert set(response.answers["angry"]) == {"noul"}
        assert set(response.answers["severity"]) == {"score", "confidence", "probabilities"}
        assert response.answers["dept"]["choice"] == "shipping"
        assert response.model == "jev-latest" and response.output_tokens == 3

    def test_score_levels_become_ascending_integers(self) -> None:
        """The API keys levels as strings; SQL wants MAP(INTEGER, DOUBLE), in order."""

        def handler(request: httpx.Request) -> httpx.Response:
            body = handle(json.loads(request.content))
            levels = body["answers"]["severity"]["probabilities"]
            body["answers"]["severity"]["probabilities"] = dict(reversed(levels.items()))
            return httpx.Response(200, json=body)

        (response,) = _ask_many(_client(handler), ["x"])
        assert response is not None
        assert list(response.answers["severity"]["probabilities"]) == [0, 1, 2]

    def test_structured_states_are_deduplicated_regardless_of_key_order(self) -> None:
        calls: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content)["state"])
            return _mock_backend(request)

        states = [{"a": 1, "b": [1, 2]}, {"b": [1, 2], "a": 1}, {"a": 2, "b": [1, 2]}, None]
        responses = _ask_many(_client(handler), states)
        assert len(calls) == 2
        assert responses[0] is responses[1] and responses[3] is None

    def test_a_string_and_the_same_text_as_an_object_are_different_states(self) -> None:
        calls: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content)["state"])
            return _mock_backend(request)

        _ask_many(_client(handler), ['{"a": 1}', {"a": 1}])
        assert calls.count('{"a": 1}') == 1 and calls.count({"a": 1}) == 1

    @pytest.mark.parametrize(
        ("mutate", "fragment"),
        [
            (lambda a: a.pop("angry"), "no 'angry' answer"),
            (lambda a: a["angry"].pop("noul"), "no numeric 'noul'"),
            (lambda a: a["angry"].update(noul="high"), "no numeric 'noul'"),
            (lambda a: a["severity"].pop("score"), "no numeric 'score'"),
            (lambda a: a["dept"].pop("choice"), "no 'choice'"),
            (lambda a: a["angry"].update(type="choice"), "asked as 'noul'"),
        ],
    )
    def test_a_malformed_answer_raises_and_names_the_question(self, mutate, fragment: str) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = handle(json.loads(request.content))
            mutate(body["answers"])
            return httpx.Response(200, json=body)

        with pytest.raises(api.TypeSafeError, match=fragment):
            _ask_many(_client(handler), ["x"])

    def test_a_response_without_answers_raises(self) -> None:
        with pytest.raises(api.TypeSafeError, match="no 'answers'"):
            _ask_many(_client(lambda request: httpx.Response(200, json={"model": "m"})), ["x"])
