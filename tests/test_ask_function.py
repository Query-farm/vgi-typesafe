# Copyright 2026 Query Farm LLC - https://query.farm

"""``ask()`` driven over the real VGI protocol, against the mock endpoint.

The worker runs as a subprocess and is driven with the framework's own client —
everything DuckDB would exercise except DuckDB itself. ``test_end_to_end.py``
covers the SQL surface; ``test_ask_logic.py`` the validation rules in detail.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest
from vgi.arguments import Arguments
from vgi.client import Client
from vgi.client.client import ClientError

from vgi_typesafe.ask import ANSWER_TYPES, USAGE_TYPE
from vgi_typesafe.mock_server import MockTypeSafeServer, running

WORKER = [sys.executable, "-m", "vgi_typesafe.worker"]

QUESTIONS: dict[str, dict[str, Any]] = {
    "dept": {
        "type": "choice",
        "instructions": "Which team should handle this?",
        "criteria": {"shipping": "Delivery status, delays, lost packages", "billing": "Charges, invoices"},
    },
    "angry": {
        "type": "noul",
        "instructions": "Is the customer angry?",
        "criteria": {"true": "furious, unacceptable", "false": "calm, thanks"},
    },
    "severity": {
        "type": "score",
        "instructions": "How bad is it?",
        "criteria": ["minor question", "disruptive delay", "critical outage"],
    },
}
TICKET = pa.struct([("message", pa.string()), ("tier", pa.string())])


@pytest.fixture
def mock() -> Iterator[MockTypeSafeServer]:
    with running(api_key="test-key") as server:
        yield server


def _call(
    mock: MockTypeSafeServer,
    states: pa.Array,  # type: ignore[type-arg]
    *,
    questions: Any = None,
    api_key: str | None = "test-key",
    **named: Any,
) -> pa.Table:
    questions = pa.scalar(QUESTIONS) if questions is None else questions
    arguments = Arguments(named={"questions": questions, **{k: pa.scalar(v) for k, v in named.items()}})
    secrets = {"typesafe": {"api_key": api_key, "base_url": mock.base_url}} if api_key else None
    with Client(WORKER) as client:
        batches = list(
            client.table_in_out_function(
                function_name="ask",
                schema_path=["main"],
                input=iter([pa.record_batch({"state": states})]),
                arguments=arguments,
                secrets=secrets,
                has_finalize=False,
            )
        )
    return pa.Table.from_batches(batches)


class TestAnswers:
    def test_one_typed_struct_per_question_then_usage(self, mock: MockTypeSafeServer) -> None:
        result = _call(mock, pa.array(["My package is lost, this is unacceptable"]))
        assert result.schema.names == ["dept", "angry", "severity", "usage"]
        assert result.schema.types == [
            ANSWER_TYPES["choice"],
            ANSWER_TYPES["noul"],
            ANSWER_TYPES["score"],
            USAGE_TYPE,
        ]

    def test_every_question_is_answered_from_a_single_request(self, mock: MockTypeSafeServer) -> None:
        (row,) = _call(mock, pa.array(["My package is lost, this is unacceptable"])).to_pylist()
        assert len(mock.requests) == 1
        assert row["dept"]["choice"] == "shipping"
        assert [option for option, _ in row["dept"]["probabilities"]] == ["shipping", "billing"]
        assert row["angry"]["noul"] > 0.5
        assert [level for level, _ in row["severity"]["probabilities"]] == [0, 1, 2]
        assert 0.0 <= row["severity"]["score"] <= 2.0
        assert row["usage"] == {
            "model": "jev-latest",
            "input_tokens": row["usage"]["input_tokens"],
            "output_tokens": 3,
        }

    def test_rows_stay_paired_with_their_own_answers(self, mock: MockTypeSafeServer) -> None:
        states = pa.array(["lost package", "invoice charges are wrong", "my delivery is delayed"])
        result = _call(mock, states)
        assert [d["choice"] for d in result.column("dept").to_pylist()] == ["shipping", "billing", "shipping"]

    def test_the_questions_reach_the_api_verbatim(self, mock: MockTypeSafeServer) -> None:
        _call(mock, pa.array(["hello"]), model="jev-1.13")
        (request,) = mock.requests
        assert request == {"state": "hello", "model": "jev-1.13", "questions": QUESTIONS}


class TestState:
    def test_a_struct_is_sent_as_a_json_object(self, mock: MockTypeSafeServer) -> None:
        states = pa.array([{"message": "lost package", "tier": "gold"}], type=TICKET)
        _call(mock, states)
        assert mock.requests[0]["state"] == {"message": "lost package", "tier": "gold"}

    def test_a_list_is_sent_as_a_json_array(self, mock: MockTypeSafeServer) -> None:
        _call(mock, pa.array([["Hi", "My card was charged twice"]], type=pa.list_(pa.string())))
        assert mock.requests[0]["state"] == ["Hi", "My card was charged twice"]

    def test_nested_maps_become_objects(self, mock: MockTypeSafeServer) -> None:
        kind = pa.struct([("message", pa.string()), ("attrs", pa.map_(pa.string(), pa.string()))])
        _call(mock, pa.array([{"message": "hi", "attrs": [("plan", "pro")]}], type=kind))
        assert mock.requests[0]["state"] == {"message": "hi", "attrs": {"plan": "pro"}}

    def test_text_is_a_string_unless_parse_json_is_set(self, mock: MockTypeSafeServer) -> None:
        text = json.dumps({"message": "lost package"})
        _call(mock, pa.array([text]))
        _call(mock, pa.array([text]), parse_json=True)
        assert [r["state"] for r in mock.requests] == [text, {"message": "lost package"}]

    def test_null_and_content_free_states_make_no_request(self, mock: MockTypeSafeServer) -> None:
        """1:1 row counts must hold or a LATERAL pairs outputs with the wrong inputs."""
        states = pa.array(
            [None, {"message": "lost package", "tier": None}, {"message": None, "tier": None}], type=TICKET
        )
        result = _call(mock, states)
        assert result.num_rows == 3
        assert [d and d["choice"] for d in result.column("dept").to_pylist()] == [None, "shipping", None]
        assert result.column("usage").to_pylist()[0] is None
        assert len(mock.requests) == 1

    def test_an_all_null_batch_needs_no_key(self, mock: MockTypeSafeServer) -> None:
        result = _call(mock, pa.array([None, None], type=TICKET), api_key=None)
        assert result.column("dept").to_pylist() == [None, None]
        assert mock.requests == []

    def test_identical_structured_states_are_billed_once(self, mock: MockTypeSafeServer) -> None:
        ticket = {"message": "lost package", "tier": "gold"}
        result = _call(mock, pa.array([ticket] * 6, type=TICKET))
        assert result.num_rows == 6 and len(mock.requests) == 1


class TestQuestionForms:
    def test_a_json_string(self, mock: MockTypeSafeServer) -> None:
        result = _call(mock, pa.array(["lost package"]), questions=pa.scalar(json.dumps(QUESTIONS)))
        assert result.schema.names == ["dept", "angry", "severity", "usage"]
        assert mock.requests[0]["questions"] == QUESTIONS

    def test_a_single_question(self, mock: MockTypeSafeServer) -> None:
        only = {"spam": {"type": "noul", "instructions": "Is this spam?"}}
        result = _call(mock, pa.array(["buy now"]), questions=pa.scalar(only))
        assert result.schema.names == ["spam", "usage"]


class TestErrorsSurface:
    @pytest.mark.parametrize(
        ("questions", "fragment"),
        [
            ({"q": {"type": "essay", "instructions": "x"}}, "expected one of choice, noul, score"),
            ({"q": {"type": "score", "instructions": "x", "criteria": ["one"]}}, "2-10 level descriptions"),
            ({"usage": {"type": "noul", "instructions": "x"}}, "may not be named 'usage'"),
        ],
    )
    def test_bad_questions_fail_at_bind_before_any_request(
        self, mock: MockTypeSafeServer, questions: dict, fragment: str
    ) -> None:
        with pytest.raises(ClientError, match=fragment):
            _call(mock, pa.array(["hello"]), questions=pa.scalar(questions))
        assert mock.requests == []

    def test_an_unsupported_state_type_fails_at_bind(self, mock: MockTypeSafeServer) -> None:
        with pytest.raises(ClientError, match="state must be VARCHAR, STRUCT, LIST or MAP"):
            _call(mock, pa.array([1, 2, 3]))
        assert mock.requests == []

    def test_invalid_json_with_parse_json_is_an_error_not_a_null(self, mock: MockTypeSafeServer) -> None:
        with pytest.raises(ClientError, match="not valid JSON"):
            _call(mock, pa.array(["{oops"]), parse_json=True)

    def test_a_rejected_key_is_an_error_not_a_null(self, mock: MockTypeSafeServer) -> None:
        with pytest.raises(ClientError, match="rejected the API key"):
            _call(mock, pa.array(["hello"]), api_key="wrong-key")
