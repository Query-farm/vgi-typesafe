# Copyright 2026 Query Farm LLC - https://query.farm

"""``ask_dynamic()`` driven over the real VGI protocol, against the mock endpoint.

The worker runs as a subprocess and is driven with the framework's own client, so
both positional arguments arrive as real input columns — which is the only way to
prove that the questions really are per-row and not quietly bound once.
``test_end_to_end.py`` covers the SQL surface; ``test_ask_dynamic_logic.py`` the
validation rules in detail.
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

from vgi_typesafe.mock_server import MockTypeSafeServer, running

WORKER = [sys.executable, "-m", "vgi_typesafe.worker"]

DEPT: dict[str, Any] = {
    "type": "choice",
    "instructions": "Which team should handle this?",
    "criteria": {"shipping": "Delivery status, delays, lost packages", "billing": "Charges, invoices"},
}
ANGRY: dict[str, Any] = {"type": "noul", "instructions": "Is the customer angry?"}
SEVERITY: dict[str, Any] = {
    "type": "score",
    "instructions": "How bad is it?",
    "criteria": ["minor question", "disruptive delay", "critical outage"],
}
TICKET = pa.struct([("message", pa.string()), ("tier", pa.string())])


@pytest.fixture
def mock() -> Iterator[MockTypeSafeServer]:
    """A mock TypeSafe endpoint on a free port, requiring a known key."""
    with running(api_key="test-key") as server:
        yield server


def _call(
    mock: MockTypeSafeServer,
    states: pa.Array,  # type: ignore[type-arg]
    questions: pa.Array,  # type: ignore[type-arg]
    *,
    api_key: str | None = "test-key",
    **named: Any,
) -> pa.Table:
    arguments = Arguments(named={k: pa.scalar(v) for k, v in named.items()})
    secrets = {"typesafe": {"api_key": api_key, "base_url": mock.base_url}} if api_key else None
    with Client(WORKER) as client:
        batches = list(
            client.table_in_out_function(
                function_name="ask_dynamic",
                schema_path=["main"],
                input=iter([pa.record_batch({"state": states, "questions": questions})]),
                arguments=arguments,
                secrets=secrets,
                has_finalize=False,
            )
        )
    return pa.Table.from_batches(batches)


def _answers(table: pa.Table) -> list[Any]:
    """Each row's `answers` column parsed back from JSON, with NULL preserved."""
    return [None if text is None else json.loads(text) for text in table.column("answers").to_pylist()]


class TestPerRowQuestions:
    """The one thing this function exists for: two rows asking different things in one scan."""

    def test_the_result_shape_is_fixed_whatever_the_questions_are(self, mock: MockTypeSafeServer) -> None:
        """It is fixed *because* the questions are not: no bind-time shape is knowable."""
        result = _call(mock, pa.array(["lost package"]), pa.array([json.dumps({"dept": DEPT})]))
        assert result.schema.names == ["answers", "model", "input_tokens", "output_tokens"]
        assert result.schema.types == [pa.string(), pa.string(), pa.int64(), pa.int64()]

    def test_each_row_is_asked_only_what_it_carried(self, mock: MockTypeSafeServer) -> None:
        """A shared question map would be invisible in the output but wrong in the bill and the answer."""
        result = _call(
            mock,
            pa.array(["my package is lost", "thanks, one invoice question"]),
            pa.array([json.dumps({"dept": DEPT}), json.dumps({"angry": ANGRY})]),
        )
        assert [sorted(row) for row in _answers(result)] == [["dept"], ["angry"]]
        # Requests are issued concurrently, so their order is not the input's.
        assert sorted(tuple(request["questions"]) for request in mock.requests) == [("angry",), ("dept",)]

    def test_a_struct_column_padded_by_unification_still_asks_one_question(self, mock: MockTypeSafeServer) -> None:
        """DuckDB pads a heterogeneous STRUCT column with NULLs; the padding must not reach the API.

        This is the form the docs warn about. It works, and this is what
        "it works" has to mean: the padded question is never sent, so a row is
        never billed for — or answered with — another row's question.
        """
        # The type DuckDB unifies a two-row questions column into: every
        # question on every row, and every question body carrying every field
        # any of them used.
        body = pa.struct(
            [("type", pa.string()), ("instructions", pa.string()), ("criteria", pa.map_(pa.string(), pa.string()))]
        )
        questions = pa.array(
            [
                {"dept": None, "angry": {**ANGRY, "criteria": None}},
                {"dept": {**DEPT, "criteria": list(DEPT["criteria"].items())}, "angry": None},
            ],
            type=pa.struct([("dept", body), ("angry", body)]),
        )
        result = _call(mock, pa.array(["thanks", "my package is lost"]), questions)
        assert [sorted(row) for row in _answers(result)] == [["angry"], ["dept"]]
        assert sorted(tuple(request["questions"]) for request in mock.requests) == [("angry",), ("dept",)]

    def test_every_question_type_round_trips_through_the_json(self, mock: MockTypeSafeServer) -> None:
        """The JSON is the whole result; a field lost in serialisation is a field a caller cannot get back."""
        questions = {"dept": DEPT, "angry": ANGRY, "severity": SEVERITY}
        (row,) = _answers(_call(mock, pa.array(["my package is lost"]), pa.array([json.dumps(questions)])))
        assert row["dept"]["choice"] == "shipping"
        assert set(row["dept"]["probabilities"]) == {"shipping", "billing"}
        assert 0.0 <= row["angry"]["noul"] <= 1.0
        assert 0.0 <= row["severity"]["score"] <= 2.0

    def test_score_levels_survive_json_as_usable_keys(self, mock: MockTypeSafeServer) -> None:
        """`ask()` gives these an INTEGER MAP key; JSON has only text keys, so they must be the digits."""
        (row,) = _answers(_call(mock, pa.array(["outage"]), pa.array([json.dumps({"sev": SEVERITY})])))
        assert sorted(row["sev"]["probabilities"]) == ["0", "1", "2"]


class TestBilling:
    """What a batch costs, which is the difference between this function and `ask()`."""

    def test_the_same_content_asked_two_things_is_two_requests(self, mock: MockTypeSafeServer) -> None:
        """De-duplicating on the state alone would answer one of the two questions twice and lose the other."""
        result = _call(
            mock,
            pa.array(["lost package", "lost package"]),
            pa.array([json.dumps({"dept": DEPT}), json.dumps({"angry": ANGRY})]),
        )
        assert [sorted(row) for row in _answers(result)] == [["dept"], ["angry"]]
        assert len(mock.requests) == 2

    def test_the_same_pair_repeated_is_one_request(self, mock: MockTypeSafeServer) -> None:
        """A LATERAL over a low-cardinality column repeats pairs; each distinct one is paid for once."""
        questions = json.dumps({"dept": DEPT})
        result = _call(mock, pa.array(["lost package"] * 5), pa.array([questions] * 5))
        assert result.num_rows == 5
        assert len(mock.requests) == 1

    def test_usage_stays_in_real_columns(self, mock: MockTypeSafeServer) -> None:
        """Cost does not vary in shape, so reading it should not cost a JSON parse."""
        (row,) = _call(mock, pa.array(["lost package"]), pa.array([json.dumps({"dept": DEPT})])).to_pylist()
        assert row["model"] == "jev-1.13.0"
        assert row["input_tokens"] > 0 and row["output_tokens"] == 1


class TestRowSemantics:
    """1:1 row counts and what is never sent, both of which a LATERAL depends on."""

    def test_a_null_state_answers_null_and_asks_nothing(self, mock: MockTypeSafeServer) -> None:
        """Wrong row counts under a LATERAL pair answers with the wrong input, silently."""
        states = pa.array([None, {"message": "lost package", "tier": None}, {"message": None, "tier": None}], TICKET)
        questions = pa.array([json.dumps({"dept": DEPT})] * 3)
        result = _call(mock, states, questions)
        assert result.num_rows == 3
        assert [row is not None for row in _answers(result)] == [False, True, False]
        assert len(mock.requests) == 1

    def test_a_row_that_asks_nothing_is_not_asked_about_its_questions(self, mock: MockTypeSafeServer) -> None:
        """A NULL state makes no request, so its questions are not data anyone is about to use.

        Validating them anyway would fail a query over a value that was never
        going to be sent.
        """
        result = _call(mock, pa.array([None, "lost package"]), pa.array([None, json.dumps({"dept": DEPT})]))
        assert [row is not None for row in _answers(result)] == [False, True]
        assert len(mock.requests) == 1

    def test_an_all_null_batch_needs_no_key(self, mock: MockTypeSafeServer) -> None:
        """Nothing is asked, so nothing should require credentials — this must not fail the query."""
        states = pa.array([None, None], type=TICKET)
        result = _call(mock, states, pa.array([None, None], type=pa.string()), api_key=None)
        assert _answers(result) == [None, None]
        assert mock.requests == []


class TestErrorsSurface:
    """Failures must reach the user as errors that name the row, never as quiet NULLs."""

    def test_a_row_with_no_questions_points_at_that_row(self, mock: MockTypeSafeServer) -> None:
        """Questions are per-row here, so locating the one bad row is what makes the error actionable."""
        states = pa.array(["first", "the row with nothing to ask", "third"])
        questions = pa.array([json.dumps({"dept": DEPT}), None, json.dumps({"dept": DEPT})])
        with pytest.raises(ClientError, match=r"row 1 of this input batch carries no questions"):
            _call(mock, states, questions)
        with pytest.raises(ClientError, match="the row with nothing to ask"):
            _call(mock, states, questions)

    def test_a_malformed_question_names_the_row_and_the_question(self, mock: MockTypeSafeServer) -> None:
        """`ask()` can say this at bind; here it can only be said mid-scan, so it has to say more."""
        broken = json.dumps({"sev": {"type": "score", "instructions": "How bad?", "criteria": ["only one level"]}})
        with pytest.raises(ClientError, match=r"score question 'sev' requires 'criteria'.*row 0 of this input batch"):
            _call(mock, pa.array(["x"]), pa.array([broken]))

    def test_a_rejected_key_is_an_error_not_a_null(self, mock: MockTypeSafeServer) -> None:
        """A NULL here is indistinguishable from a NULL input, so the query would look like it worked."""
        with pytest.raises(ClientError, match="rejected the API key"):
            _call(mock, pa.array(["hello"]), pa.array([json.dumps({"dept": DEPT})]), api_key="wrong-key")
