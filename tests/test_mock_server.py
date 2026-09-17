"""The mocked endpoint must be indistinguishable from production on the wire.

If it accepted requests the real API rejects, the worker's tests would pass
against a contract that does not exist.
"""

from __future__ import annotations

import httpx
import pytest

from vgi_typesafe import mock_server
from vgi_typesafe.mock_server import RequestInvalid, answer_choice, handle, running

CRITERIA = {
    "returns": "Exchanges, refunds, wrong or damaged items",
    "shipping": "Delivery status, delays, lost packages",
    "billing": "Charges, invoices, payment problems",
}


def _request(state: object = "My package is lost", **question: object) -> dict:
    base = {"type": "choice", "instructions": "Which team?", "criteria": CRITERIA}
    return {"state": state, "model": "jev-latest", "questions": {"department": {**base, **question}}}


class TestScoring:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("My package never arrived and the delivery is delayed", "shipping"),
            ("I was charged twice on my invoice", "billing"),
            ("I want a refund for a damaged item", "returns"),
        ],
    )
    def test_keyword_overlap_picks_the_obvious_option(self, state: str, expected: str) -> None:
        assert answer_choice(state, {"criteria": CRITERIA})["choice"] == expected

    def test_probabilities_cover_every_option_and_sum_to_one(self) -> None:
        answer = answer_choice("lost package", {"criteria": CRITERIA})
        assert set(answer["probabilities"]) == set(CRITERIA)
        assert sum(answer["probabilities"].values()) == pytest.approx(1.0, abs=1e-5)
        assert answer["probabilities"][answer["choice"]] == max(answer["probabilities"].values())

    def test_no_signal_is_a_uniform_zero_confidence_tie_to_the_first_option(self) -> None:
        answer = answer_choice("zzz qqq", {"criteria": CRITERIA})
        assert answer["choice"] == "returns"
        assert answer["confidence"] == pytest.approx(0.0, abs=1e-5)

    def test_more_evidence_means_more_confidence(self) -> None:
        weak = answer_choice("package", {"criteria": CRITERIA})
        strong = answer_choice("lost package, delivery delays", {"criteria": CRITERIA})
        assert 0.0 < weak["confidence"] < strong["confidence"] <= 1.0

    def test_structured_not_for_counts_against_an_option(self) -> None:
        criteria = {
            "return_policy": {"what": "Whether an item can be returned", "not_for": "tracking a return"},
            "return_status": {"what": "Tracking progress of a return already sent"},
        }
        assert answer_choice("tracking my return", {"criteria": criteria})["choice"] == "return_status"

    def test_a_json_object_state_is_searched_in_full(self) -> None:
        state = {"subject": "help", "body": {"text": "invoice charges are wrong"}}
        assert answer_choice(state, {"criteria": CRITERIA})["choice"] == "billing"

    def test_is_deterministic(self) -> None:
        assert handle(_request()) == handle(_request())


class TestValidation:
    def test_response_shape(self) -> None:
        body = handle(_request())
        assert body["model"] == "jev-latest"
        assert body["answers"]["department"]["type"] == "choice"
        assert set(body["usage"]) == {"input_tokens", "output_tokens"}

    def test_every_question_in_a_request_is_answered(self) -> None:
        request = _request()
        request["questions"]["second"] = request["questions"]["department"]
        assert set(handle(request)["answers"]) == {"department", "second"}

    @pytest.mark.parametrize(
        ("mutate", "fragment"),
        [
            (lambda r: r.pop("state"), "'state'"),
            (lambda r: r.pop("model"), "'model'"),
            (lambda r: r.update(questions={}), "'questions'"),
            (lambda r: r["questions"]["department"].update(type="score"), "only answers type 'choice'"),
            (lambda r: r["questions"]["department"].update(instructions=" "), "'instructions'"),
            (lambda r: r["questions"]["department"].update(criteria={}), "'criteria'"),
            (
                lambda r: r["questions"]["department"].update(criteria={f"o{i}": "" for i in range(256)}),
                "at most 255",
            ),
        ],
    )
    def test_invalid_requests_are_rejected(self, mutate, fragment: str) -> None:
        request = _request()
        mutate(request)
        with pytest.raises(RequestInvalid, match=fragment):
            handle(request)


class TestOverHttp:
    def test_a_valid_request_round_trips(self) -> None:
        with running(api_key="k") as server:
            response = httpx.post(
                f"{server.base_url}/v1/systemone", json=_request(), headers={"Authorization": "Bearer k"}
            )
        assert response.status_code == 200
        assert response.json()["answers"]["department"]["choice"] == "shipping"
        assert len(server.requests) == 1

    @pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "k"}])
    def test_a_bad_key_is_a_401_and_is_not_recorded(self, headers: dict[str, str]) -> None:
        with running(api_key="k") as server:
            response = httpx.post(f"{server.base_url}/v1/systemone", json=_request(), headers=headers)
        assert response.status_code == 401
        assert server.requests == []

    def test_without_a_configured_key_any_bearer_token_is_accepted(self) -> None:
        with running() as server:
            response = httpx.post(
                f"{server.base_url}/v1/systemone", json=_request(), headers={"Authorization": "Bearer x"}
            )
        assert response.status_code == 200

    def test_validation_failure_is_a_422_with_a_detail(self) -> None:
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
        with running() as server:
            response = httpx.post(
                f"{server.base_url}/v1/nope", json={}, headers={"Authorization": "Bearer x"}
            )
        assert response.status_code == 404


def test_the_mock_agrees_with_the_worker_on_the_wire_contract() -> None:
    from vgi_typesafe import typesafe_api

    assert mock_server.SYSTEM_ONE_PATH == typesafe_api.SYSTEM_ONE_PATH
    assert mock_server.MAX_OPTIONS == typesafe_api.MAX_OPTIONS
