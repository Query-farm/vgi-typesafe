# Copyright 2026 Query Farm LLC - https://query.farm

"""The mocked endpoint must be indistinguishable from production on the wire.

If it accepted requests the real API rejects, the worker's tests would pass
against a contract that does not exist.
"""

from __future__ import annotations

import httpx
import pytest

from vgi_typesafe import mock_server
from vgi_typesafe.mock_server import (
    RequestInvalid,
    answer_choice,
    answer_noul,
    answer_score,
    handle,
    running,
)

CRITERIA = {
    "returns": "Exchanges, refunds, wrong or damaged items",
    "shipping": "Delivery status, delays, lost packages",
    "billing": "Charges, invoices, payment problems",
}


def _request(state: object = "My package is lost", **question: object) -> dict:
    base = {"type": "choice", "instructions": "Which team?", "criteria": CRITERIA}
    return {"state": state, "model": "jev-latest", "questions": {"department": {**base, **question}}}


class TestScoring:
    """Choice scoring: deterministic, so tests can assert exact options."""

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("My package never arrived and the delivery is delayed", "shipping"),
            ("I was charged twice on my invoice", "billing"),
            ("I want a refund for a damaged item", "returns"),
        ],
    )
    def test_keyword_overlap_picks_the_obvious_option(self, state: str, expected: str) -> None:
        """The scoring must be good enough to assert real answers on."""
        assert answer_choice(state, {"criteria": CRITERIA})["choice"] == expected

    def test_probabilities_cover_every_option_and_sum_to_one(self) -> None:
        """A distribution, not a score per option."""
        answer = answer_choice("lost package", {"criteria": CRITERIA})
        assert set(answer["probabilities"]) == set(CRITERIA)
        assert sum(answer["probabilities"].values()) == pytest.approx(1.0, abs=1e-5)
        assert answer["probabilities"][answer["choice"]] == max(answer["probabilities"].values())

    def test_no_signal_is_a_uniform_zero_confidence_tie_to_the_first_option(self) -> None:
        """Ties must not depend on dict ordering, or results stop being reproducible."""
        answer = answer_choice("zzz qqq", {"criteria": CRITERIA})
        assert answer["choice"] == "returns"
        assert answer["confidence"] == pytest.approx(0.0, abs=1e-5)

    def test_more_evidence_means_more_confidence(self) -> None:
        """Confidence has to track evidence to be worth filtering on."""
        weak = answer_choice("package", {"criteria": CRITERIA})
        strong = answer_choice("lost package, delivery delays", {"criteria": CRITERIA})
        assert 0.0 < weak["confidence"] < strong["confidence"] <= 1.0

    def test_structured_not_for_counts_against_an_option(self) -> None:
        """`not_for` is what disambiguates options that otherwise look alike."""
        criteria = {
            "return_policy": {"what": "Whether an item can be returned", "not_for": "tracking a return"},
            "return_status": {"what": "Tracking progress of a return already sent"},
        }
        assert answer_choice("tracking my return", {"criteria": criteria})["choice"] == "return_status"

    def test_a_json_object_state_is_searched_in_full(self) -> None:
        """An object state must be judged whole, not by one field."""
        state = {"subject": "help", "body": {"text": "invoice charges are wrong"}}
        assert answer_choice(state, {"criteria": CRITERIA})["choice"] == "billing"

    def test_is_deterministic(self) -> None:
        """Every assertion in the suite rests on this."""
        assert handle(_request()) == handle(_request())


SCALE = ["minor cosmetic question", "disruptive delay", "critical outage, data loss"]


class TestScoreScoring:
    """Score questions: position on an ordered scale."""

    @pytest.mark.parametrize(
        ("state", "level"),
        [("a cosmetic question", 0), ("a disruptive delay", 1), ("critical outage with data loss", 2)],
    )
    def test_the_matching_level_dominates(self, state: str, level: int) -> None:
        """The scale has to respond to the state or scores mean nothing."""
        answer = answer_score(state, {"criteria": SCALE})
        assert max(answer["probabilities"], key=answer["probabilities"].get) == str(level)
        assert abs(answer["score"] - level) < 0.5

    def test_score_is_the_probability_weighted_level(self) -> None:
        """Matches how TypeSafe documents a fractional score."""
        answer = answer_score("disruptive delay", {"criteria": SCALE})
        expected = sum(int(level) * p for level, p in answer["probabilities"].items())
        assert answer["score"] == pytest.approx(expected, abs=1e-5)
        assert sum(answer["probabilities"].values()) == pytest.approx(1.0, abs=1e-5)

    def test_levels_are_keyed_by_number_and_the_legend_maps_them_back(self) -> None:
        """Levels are positions; the legend is what makes a number readable again."""
        answer = answer_score("x", {"criteria": SCALE})
        assert list(answer["probabilities"]) == ["0", "1", "2"]
        assert answer["legend"] == {"0": SCALE[0], "1": SCALE[1], "2": SCALE[2]}

    def test_no_signal_lands_mid_scale_with_zero_confidence(self) -> None:
        """Undecided means the middle, flagged as undecided."""
        answer = answer_score("zzz", {"criteria": SCALE})
        assert answer["score"] == pytest.approx(1.0, abs=1e-5)
        assert answer["confidence"] == pytest.approx(0.0, abs=1e-5)

    def test_structured_levels_are_accepted(self) -> None:
        """A level may be prose or a {what, examples} object."""
        levels = [{"what": "minor", "examples": ["typo"]}, {"what": "major", "examples": ["outage"]}]
        answer = answer_score("there is an outage", {"criteria": levels})
        assert answer["score"] > 0.5
        assert answer["legend"] == {"0": "minor", "1": "major"}


class TestNoulScoring:
    """Noul questions: a yes/no probability."""

    QUESTION = {
        "instructions": "Is the customer angry?",
        "criteria": {"true": "furious, unacceptable", "false": "calm, thanks, happy"},
    }

    def test_evidence_for_pushes_toward_one(self) -> None:
        """Near 1 is yes."""
        assert answer_noul("This is unacceptable, I am furious", self.QUESTION)["noul"] > 0.9

    def test_evidence_against_pushes_toward_zero(self) -> None:
        """Near 0 is no."""
        assert answer_noul("Thanks, I am happy and calm", self.QUESTION)["noul"] < 0.1

    def test_no_evidence_reads_as_probably_not(self) -> None:
        """Absence of evidence is weak evidence of absence, not 0.5."""
        assert answer_noul("zzz", self.QUESTION)["noul"] == pytest.approx(0.268941, abs=1e-6)

    def test_criteria_are_optional(self) -> None:
        """A noul question can stand on its instructions alone."""
        question = {"instructions": "Is this about an invoice?"}
        assert answer_noul("my invoice is wrong", question)["noul"] > 0.5
        assert answer_noul("hello there", question)["noul"] < 0.5

    def test_is_a_probability(self) -> None:
        """Whatever the input, the answer stays in range."""
        for state in ("", "furious " * 50, "thanks " * 50):
            assert 0.0 <= answer_noul(state, self.QUESTION)["noul"] <= 1.0


class TestMixedRequest:
    """All three types in one request, as `ask()` sends them."""

    def test_every_type_is_answered_in_one_request(self) -> None:
        """The behaviour `ask()` is built on."""
        request = {
            "state": {"message": "critical outage, I am furious", "tier": "gold"},
            "model": "jev-latest",
            "questions": {
                "dept": {"type": "choice", "instructions": "Which team?", "criteria": CRITERIA},
                "angry": {"type": "noul", "instructions": "Is the customer furious?"},
                "severity": {"type": "score", "instructions": "How bad?", "criteria": SCALE},
            },
        }
        answers = handle(request)["answers"]
        assert {name: a["type"] for name, a in answers.items()} == {
            "dept": "choice",
            "angry": "noul",
            "severity": "score",
        }
        assert answers["angry"]["noul"] > 0.5 and answers["severity"]["score"] > 1.5

    @pytest.mark.parametrize(
        ("question", "fragment"),
        [
            ({"type": "score", "instructions": "x", "criteria": ["only one"]}, "2-10 levels"),
            ({"type": "score", "instructions": "x", "criteria": [str(i) for i in range(11)]}, "2-10 levels"),
            ({"type": "score", "instructions": "x", "criteria": {"a": "b"}}, "ordered array"),
            ({"type": "score", "instructions": "x", "criteria": ["ok", 3]}, "string or an object"),
            (
                {"type": "noul", "instructions": "x", "criteria": {"maybe": "y"}},
                "only have 'true' and 'false'",
            ),
            ({"type": "noul", "instructions": "x", "criteria": ["y"]}, "only have 'true' and 'false'"),
            ({"type": "choice", "instructions": "x", "criteria": ["a", "b"]}, "non-empty object"),
        ],
    )
    def test_each_type_validates_its_own_criteria(self, question: dict, fragment: str) -> None:
        """The shapes differ per type, so the validation must too."""
        with pytest.raises(RequestInvalid, match=fragment):
            handle({"state": "s", "model": "m", "questions": {"q": question}})


class TestValidation:
    """Requests the real API rejects must be rejected here too."""

    def test_response_shape(self) -> None:
        """The envelope the client parses."""
        body = handle(_request())
        assert body["model"] == "jev-latest"
        assert body["answers"]["department"]["type"] == "choice"
        assert set(body["usage"]) == {"input_tokens", "output_tokens"}

    def test_every_question_in_a_request_is_answered(self) -> None:
        """A missing answer would surface as a confusing client error."""
        request = _request()
        request["questions"]["second"] = request["questions"]["department"]
        assert set(handle(request)["answers"]) == {"department", "second"}

    @pytest.mark.parametrize(
        ("mutate", "fragment"),
        [
            (lambda r: r.pop("state"), "'state'"),
            (lambda r: r.pop("model"), "'model'"),
            (lambda r: r.update(questions={}), "'questions'"),
            (lambda r: r["questions"]["department"].update(type="essay"), "'type' must be one of"),
            (lambda r: r["questions"]["department"].update(type=None), "'type' must be one of"),
            (lambda r: r["questions"]["department"].update(instructions=" "), "'instructions'"),
            (lambda r: r["questions"]["department"].update(criteria={}), "'criteria'"),
            (
                lambda r: r["questions"]["department"].update(criteria={f"o{i}": "" for i in range(256)}),
                "at most 255",
            ),
        ],
    )
    def test_invalid_requests_are_rejected(self, mutate, fragment: str) -> None:
        """Accepting what the real API rejects would make the tests lie."""
        request = _request()
        mutate(request)
        with pytest.raises(RequestInvalid, match=fragment):
            handle(request)


class TestOverHttp:
    """The wire behaviour a client actually meets."""

    def test_a_valid_request_round_trips(self) -> None:
        """The happy path over real HTTP, not just in-process."""
        with running(api_key="k") as server:
            response = httpx.post(
                f"{server.base_url}/v1/systemone", json=_request(), headers={"Authorization": "Bearer k"}
            )
        assert response.status_code == 200
        assert response.json()["answers"]["department"]["choice"] == "shipping"
        assert len(server.requests) == 1

    @pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "k"}])
    def test_a_bad_key_is_a_401_and_is_not_recorded(self, headers: dict[str, str]) -> None:
        """A rejected request must not look like a served one."""
        with running(api_key="k") as server:
            response = httpx.post(f"{server.base_url}/v1/systemone", json=_request(), headers=headers)
        assert response.status_code == 401
        assert server.requests == []

    def test_without_a_configured_key_any_bearer_token_is_accepted(self) -> None:
        """Convenience for tests that do not care about auth."""
        with running() as server:
            response = httpx.post(
                f"{server.base_url}/v1/systemone", json=_request(), headers={"Authorization": "Bearer x"}
            )
        assert response.status_code == 200

    def test_validation_failure_is_a_422_with_a_detail(self) -> None:
        """The detail is what the worker surfaces to the user."""
        with running() as server:
            response = httpx.post(
                f"{server.base_url}/v1/systemone",
                json={"state": "x"},
                headers={"Authorization": "Bearer x"},
            )
        assert response.status_code == 422
        assert "'model'" in response.json()["detail"]

    def test_a_rejection_does_not_poison_the_keep_alive_connection(self) -> None:
        """An early 401 must still drain the body, or the next request on the socket breaks."""
        with running(api_key="k") as server, httpx.Client() as client:
            url = f"{server.base_url}/v1/systemone"
            assert client.post(url, json=_request()).status_code == 401
            assert client.post(url, json=_request(), headers={"Authorization": "Bearer k"}).status_code == 200

    def test_other_paths_are_404(self) -> None:
        """A typo'd base_url should fail loudly, not silently."""
        with running() as server:
            response = httpx.post(f"{server.base_url}/v1/nope", json={}, headers={"Authorization": "Bearer x"})
        assert response.status_code == 404


def test_the_mock_agrees_with_the_worker_on_the_wire_contract() -> None:
    """Two copies of a constant drift; this fails the moment they do."""
    from vgi_typesafe import typesafe_api

    assert mock_server.SYSTEM_ONE_PATH == typesafe_api.SYSTEM_ONE_PATH
    assert mock_server.MAX_OPTIONS == typesafe_api.MAX_OPTIONS
