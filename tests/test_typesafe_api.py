# Copyright 2026 Query Farm LLC - https://query.farm

"""The HTTP layer: wire format, retries, errors, and per-batch de-duplication."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from vgi_typesafe import typesafe_api as api
from vgi_typesafe.auth import Credentials
from vgi_typesafe.mock_server import handle
from vgi_typesafe.mock_server import list_models as mock_list_models

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
    """The request we put on the wire and the answer we parse back."""

    def test_request_matches_the_api_reference(self) -> None:
        """Pinned against the published shape, not against our own helpers."""
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
        """Every field a caller reads must survive the round trip."""
        (answer,) = _ask(_client(_mock_backend), ["I was charged twice on my invoice"])
        assert answer is not None
        assert answer.choice == "billing"
        assert 0.0 < answer.confidence <= 1.0
        assert answer.model == "jev-1.13.0"
        assert answer.input_tokens and answer.output_tokens == 1

    def test_probabilities_follow_the_callers_option_order(self) -> None:
        """Reported in criteria order so the MAP reads the same on every row."""

        def handler(request: httpx.Request) -> httpx.Response:
            answer = {"type": "choice", "choice": "billing", "confidence": 1.0}
            answer["probabilities"] = {"billing": 1.0, "shipping": 0.0}  # reversed on purpose
            return httpx.Response(200, json={"model": "m", "answers": {"choice": answer}})

        (answer,) = _ask(_client(handler), ["x"])
        assert answer is not None and list(answer.probabilities) == ["shipping", "billing"]


class TestBatching:
    """Per-batch de-duplication, ordering and concurrency."""

    def test_results_are_in_input_order(self) -> None:
        """De-duplication must not reorder the results."""
        states = ["lost package", "invoice charges", "lost package delivery"]
        answers = _ask(_client(_mock_backend), states)
        assert [a.choice for a in answers if a] == ["shipping", "billing", "shipping"]

    def test_null_states_make_no_request(self) -> None:
        """A NULL row should cost nothing."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("should not have been called")

        assert _ask(_client(handler), [None, None]) == [None, None]

    def test_nulls_keep_their_position(self) -> None:
        """Positions must survive, or answers land on the wrong rows."""
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
        """Parallelism is an optimisation, not a behaviour change."""
        states = [f"lost package {i}" if i % 2 else f"invoice {i}" for i in range(20)]
        answers = _ask(_client(_mock_backend), states, concurrency=concurrency)
        assert [a.choice for a in answers if a] == ["shipping" if i % 2 else "billing" for i in range(20)]


class TestRetries:
    """What is retried, how often, and what is not."""

    @pytest.mark.parametrize("status", [429, 529])
    def test_rate_limit_and_overload_are_retried(self, status: int, _no_sleep: list[float]) -> None:
        """The two statuses the API documents as transient."""
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(status) if len(attempts) < 3 else _mock_backend(request)

        (answer,) = _ask(_client(handler), ["lost package"])
        assert answer is not None and answer.choice == "shipping"
        assert len(attempts) == 3
        # Jittered, so assert the shape rather than exact values: each delay sits
        # inside its jitter window, and the second is longer than the first.
        first, second = _no_sleep
        assert 0.5 * (1 - api.BACKOFF_JITTER) <= first <= 0.5
        assert 1.0 * (1 - api.BACKOFF_JITTER) <= second <= 1.0
        assert second > first

    @pytest.mark.parametrize("status", [500, 502, 503, 529])
    def test_server_errors_are_retried(self, status: int, _no_sleep: list[float]) -> None:
        """The SDK retries the whole 5xx range, not just the 529 the API reference names."""
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(status) if len(attempts) < 2 else _mock_backend(request)

        (answer,) = _ask(_client(handler), ["lost package"])
        assert answer is not None and len(attempts) == 2

    def test_a_transport_failure_is_retried_before_giving_up(self, _no_sleep: list[float]) -> None:
        """A dropped connection is as transient as a 503; only the last attempt raises."""
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 3:
                raise httpx.ConnectError("connection reset")
            return _mock_backend(request)

        (answer,) = _ask(_client(handler), ["lost package"])
        assert answer is not None and len(attempts) == 3

    def test_retry_after_ms_is_honoured(self, _no_sleep: list[float]) -> None:
        """Some proxies send the millisecond spelling; the SDK reads both."""
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(429, headers={"retry-after-ms": "2500"})
            return _mock_backend(request)

        _ask(_client(handler), ["x"])
        assert _no_sleep == [2.5]

    def test_an_unparseable_retry_after_falls_back_to_backoff(self, _no_sleep: list[float]) -> None:
        """A date-formatted Retry-After is legal; it must not crash or mean zero."""
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
            return _mock_backend(request)

        _ask(_client(handler), ["x"])
        assert _no_sleep and 0 < _no_sleep[0] <= 0.5

    def test_retry_after_is_honoured(self, _no_sleep: list[float]) -> None:
        """Ignoring the server's own backoff is how you get rate-limited harder."""
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(429, headers={"Retry-After": "3"})
            return _mock_backend(request)

        _ask(_client(handler), ["x"])
        assert _no_sleep == [3.0]

    def test_retries_are_bounded(self) -> None:
        """An unbounded retry turns a rate limit into a hang."""
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(429, json={"detail": "slow down"})

        with pytest.raises(api.TypeSafeError, match="HTTP 429: slow down") as excinfo:
            _ask(_client(handler), ["x"])
        assert excinfo.value.status == 429
        assert len(attempts) == api.MAX_ATTEMPTS

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_client_errors_are_not_retried(self, status: int) -> None:
        """Retrying a 401 or a 422 just spends money to be told the same thing."""
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(status, json={"detail": "nope"})

        with pytest.raises(api.TypeSafeError, match="nope"):
            _ask(_client(handler), ["x"])
        assert len(attempts) == 1


class TestErrors:
    """Every failure must raise rather than degrade to NULL."""

    def test_a_401_names_the_key_but_never_prints_it(self) -> None:
        """Error text reaches logs; the key must not."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "invalid or missing API key"})

        with pytest.raises(api.TypeSafeError, match="rejected the API key") as excinfo:
            _ask(_client(handler), ["x"])
        assert "secret-key" not in str(excinfo.value)

    def test_a_connection_failure_is_wrapped(self) -> None:
        """A bare httpx error does not tell a SQL user which endpoint failed."""

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
        """A 200 with the wrong body is still a failure, and must not look like a NULL input."""
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
    """The general path: several questions, arbitrary states."""

    def test_all_questions_travel_in_one_request(self) -> None:
        """One request per state regardless of how many questions."""
        seen: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return _mock_backend(request)

        _ask_many(_client(handler), [{"message": "lost package", "tier": "gold"}])

        (payload,) = seen
        assert payload["state"] == {"message": "lost package", "tier": "gold"}
        assert payload["questions"] == QUESTIONS

    def test_each_answer_is_shaped_by_its_question_type(self) -> None:
        """The parser must key off the question, not guess."""
        (response,) = _ask_many(_client(_mock_backend), ["furious about this outage and lost package"])
        assert response is not None
        assert set(response.answers["dept"]) == {"choice", "confidence", "probabilities"}
        assert set(response.answers["angry"]) == {"noul"}
        assert set(response.answers["severity"]) == {"score", "confidence", "probabilities"}
        assert response.answers["dept"]["choice"] == "shipping"
        assert response.model == "jev-1.13.0" and response.output_tokens == 3

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
        """Two structs differing only in key order are the same state and must be billed once."""
        calls: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content)["state"])
            return _mock_backend(request)

        states = [{"a": 1, "b": [1, 2]}, {"b": [1, 2], "a": 1}, {"a": 2, "b": [1, 2]}, None]
        responses = _ask_many(_client(handler), states)
        assert len(calls) == 2
        assert responses[0] is responses[1] and responses[3] is None

    def test_a_string_and_the_same_text_as_an_object_are_different_states(self) -> None:
        """Collapsing these would send one and answer the other."""
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
        """With several questions in flight, a message that does not name one is unusable."""

        def handler(request: httpx.Request) -> httpx.Response:
            body = handle(json.loads(request.content))
            mutate(body["answers"])
            return httpx.Response(200, json=body)

        with pytest.raises(api.TypeSafeError, match=fragment):
            _ask_many(_client(handler), ["x"])

    def test_a_response_without_answers_raises(self) -> None:
        """A response we cannot read is an error, not an empty result."""
        with pytest.raises(api.TypeSafeError, match="no 'answers'"):
            _ask_many(_client(lambda request: httpx.Response(200, json={"model": "m"})), ["x"])


ONE_NOUL: dict[str, dict[str, Any]] = {"angry": {"type": "noul", "instructions": "Is the customer angry?"}}


class TestAskPairs:
    """De-duplication keyed on the (state, questions) pair, which `ask_dynamic()` needs and `ask_many` cannot do.

    `ask_many` keys a batch on the state alone, which is correct only because
    every row asks the same thing. Once the questions vary per row that
    assumption silently answers one row with another row's question, so these
    pin the pair — and pin that `ask_many` still rides on the same path rather
    than growing a second one.
    """

    def _calls(self, pairs: list[api.Pair | None], **kwargs: Any) -> tuple[list[Any], list[api.Response | None]]:
        seen: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return _mock_backend(request)

        responses = api.ask_pairs(pairs, credentials=CREDENTIALS, client=_client(handler), **kwargs)
        return seen, responses

    def test_one_state_asked_two_things_is_two_requests(self) -> None:
        """The case that makes state-only de-duplication wrong: one answer would be lost entirely."""
        seen, responses = self._calls([("lost package", QUESTIONS), ("lost package", ONE_NOUL)])
        # Requests go out concurrently, so compare the set of bodies, not their order.
        assert sorted(tuple(sorted(call["questions"])) for call in seen) == sorted(
            [tuple(sorted(QUESTIONS)), ("angry",)]
        )
        assert responses[0] is not responses[1]

    def test_the_same_pair_twice_is_one_request_and_one_shared_answer(self) -> None:
        """Sharing the object, not just the value, is what keeps a repeated pair from being billed twice."""
        seen, responses = self._calls([("lost package", ONE_NOUL), ("lost package", ONE_NOUL)])
        assert len(seen) == 1
        assert responses[0] is responses[1]

    def test_question_order_does_not_split_a_pair(self) -> None:
        """The API keys its answers by name, so a reordered map is the same request and must be billed once."""
        reversed_questions = dict(reversed(list(QUESTIONS.items())))
        seen, _ = self._calls([("lost package", QUESTIONS), ("lost package", reversed_questions)])
        assert len(seen) == 1

    def test_state_key_order_does_not_split_a_pair(self) -> None:
        """`ask_many` already promised this for states; routing through pairs must not quietly drop it."""
        seen, _ = self._calls([({"a": 1, "b": 2}, ONE_NOUL), ({"b": 2, "a": 1}, ONE_NOUL)])
        assert len(seen) == 1

    def test_a_none_entry_makes_no_request_and_answers_none(self) -> None:
        """The row-level NULL state, expressed where the batching happens rather than at each caller."""
        seen, responses = self._calls([None, ("lost package", ONE_NOUL), None])
        assert len(seen) == 1
        assert [r is None for r in responses] == [True, False, True]

    def test_an_entirely_empty_batch_issues_nothing(self) -> None:
        """An all-NULL batch must succeed without credentials, so it must not reach the client at all."""
        seen, responses = self._calls([None, None])
        assert seen == [] and responses == [None, None]

    def test_ask_many_is_this_function_with_one_question_map(self) -> None:
        """One batching and retry path, or the two would drift apart exactly where money is spent."""
        seen: list[Any] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return _mock_backend(request)

        client = _client(handler)
        many = api.ask_many(["lost package", None], ONE_NOUL, credentials=CREDENTIALS, client=client)
        pairs = api.ask_pairs([("lost package", ONE_NOUL), None], credentials=CREDENTIALS, client=client)
        assert [r is None for r in many] == [r is None for r in pairs]
        assert seen[0] == seen[1], "the same body either way"


def _models_backend(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=mock_list_models())


class TestListModels:
    """`GET /v1/models` — the other endpoint, sharing this module's one request path."""

    def test_request_matches_the_api_reference(self) -> None:
        """A GET with a body, or without the Bearer header, is a different request than documented."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return _models_backend(request)

        api.list_models(credentials=CREDENTIALS, client=_client(handler))

        (request,) = seen
        assert request.method == "GET"
        assert str(request.url) == "http://typesafe.test/v1/models"
        assert request.headers["authorization"] == "Bearer secret-key"
        assert request.content == b"", "a GET must not carry a JSON body"

    def test_every_field_survives_the_round_trip(self) -> None:
        """These three fields are the function's entire output; a dropped one is an empty column."""
        models = api.list_models(credentials=CREDENTIALS, client=_client(_models_backend))
        assert [m.name for m in models] == ["jev-latest", "jev-preview"]
        assert all(m.description for m in models)
        assert models[0].release_date == datetime(2026, 9, 10, 18, 38, 1, 391457, tzinfo=UTC)

    def test_the_apis_own_order_is_preserved(self) -> None:
        """Sorting here would silently override an ORDER BY-free query's ordering."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"models": [{"name": "z-model"}, {"name": "a-model"}]})

        assert [m.name for m in api.list_models(credentials=CREDENTIALS, client=_client(handler))] == [
            "z-model",
            "a-model",
        ]

    def test_a_missing_description_is_an_empty_one_not_a_failure(self) -> None:
        """A model with no blurb is still a model you can name in `model =>`."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"models": [{"name": "jev-x"}]})

        (model,) = api.list_models(credentials=CREDENTIALS, client=_client(handler))
        assert model.description == "" and model.release_date is None

    @pytest.mark.parametrize("raw", ["", "not a date", "2026-13-45", None, 17])
    def test_an_unreadable_release_date_costs_one_cell_not_the_listing(self, raw: Any) -> None:
        """Losing every model because one timestamp is odd would defeat the point of the call."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"models": [{"name": "jev-x", "release_date": raw}]})

        (model,) = api.list_models(credentials=CREDENTIALS, client=_client(handler))
        assert model.name == "jev-x" and model.release_date is None

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        """The column is TIMESTAMP WITH TIME ZONE; a naive value must not become local time."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"models": [{"name": "jev-x", "release_date": "2026-09-10T18:38:01"}]})

        (model,) = api.list_models(credentials=CREDENTIALS, client=_client(handler))
        assert model.release_date == datetime(2026, 9, 10, 18, 38, 1, tzinfo=UTC)

    @pytest.mark.parametrize(
        ("body", "fragment"),
        [
            ({"model": "jev-latest"}, "no 'models'"),
            ({"models": {"jev-latest": {}}}, "no 'models'"),
            ({"models": ["jev-latest"]}, "not an object"),
            ({"models": [{"description": "no name"}]}, "no 'name'"),
            ({"models": [{"name": "  "}]}, "no 'name'"),
        ],
    )
    def test_a_malformed_listing_raises_rather_than_returning_nothing(self, body: Any, fragment: str) -> None:
        """An empty result would read as "this account has no models", which is a different fact."""
        with pytest.raises(api.TypeSafeError, match=fragment):
            api.list_models(credentials=CREDENTIALS, client=_client(lambda request: httpx.Response(200, json=body)))

    def test_it_shares_the_retry_policy_rather_than_reimplementing_it(self, _no_sleep: list[float]) -> None:
        """Discovery failing differently from the calls it informs is the bug this guards."""
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(429) if len(attempts) < 3 else _models_backend(request)

        models = api.list_models(credentials=CREDENTIALS, client=_client(handler))
        assert len(models) == 2 and len(attempts) == 3
        assert _no_sleep, "a retried GET must back off, exactly as a retried POST does"

    def test_a_rejected_key_is_named_the_same_way(self) -> None:
        """One error vocabulary across both endpoints; a user should not have to learn two."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "invalid or missing API key"})

        with pytest.raises(api.TypeSafeError, match="rejected the API key") as excinfo:
            api.list_models(credentials=CREDENTIALS, client=_client(handler))
        assert excinfo.value.status == 401
        assert "secret-key" not in str(excinfo.value)
