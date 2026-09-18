# Copyright 2026 Query Farm LLC - https://query.farm

"""``noul()`` driven over the real VGI protocol, against the mock endpoint.

This spawns the worker as a subprocess and talks to it with the framework's own
client — everything DuckDB would exercise except DuckDB itself, so it runs
without the C++ extension. ``test_end_to_end.py`` covers the SQL surface.

The behaviour worth guarding here is what makes a noul *different* from a
choice: the answer is one number with no confidence beside it, and the criteria
are optional and describe two fixed outcomes rather than a set to pick from.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any

import pyarrow as pa
import pytest
from vgi.arguments import Arguments
from vgi.client import Client
from vgi.client.client import ClientError

from vgi_typesafe.mock_server import MockTypeSafeServer, running
from vgi_typesafe.noul import NOUL_SCHEMA, NoulFunction, criteria_of, question_of

WORKER = [sys.executable, "-m", "vgi_typesafe.worker"]
INSTRUCTIONS = "Is this message about a lost package?"
CRITERIA_TYPE = pa.map_(pa.string(), pa.string())


@pytest.fixture
def mock() -> Iterator[MockTypeSafeServer]:
    """A mock TypeSafe endpoint on a free port, requiring a known key."""
    with running(api_key="test-key") as server:
        yield server


def _arguments(**overrides: Any) -> Arguments:
    named = {"instructions": pa.scalar(INSTRUCTIONS), **overrides}
    return Arguments(named={k: v for k, v in named.items() if v is not None})


def _call(
    mock: MockTypeSafeServer,
    states: list[str | None],
    *,
    arguments: Arguments | None = None,
    api_key: str | None = "test-key",
) -> pa.Table:
    batch = pa.RecordBatch.from_pydict({"state": pa.array(states, type=pa.string())})
    secrets = {"typesafe": {"api_key": api_key, "base_url": mock.base_url}} if api_key else None
    with Client(WORKER) as client:
        batches = list(
            client.table_in_out_function(
                function_name="noul",
                schema_path=["main"],
                input=iter([batch]),
                arguments=arguments or _arguments(),
                secrets=secrets,
                has_finalize=False,
            )
        )
    return pa.Table.from_batches(batches)


class TestNoul:
    """The 1->1 contract and the columns a caller reads."""

    def test_one_output_row_per_input_row_in_order(self, mock: MockTypeSafeServer) -> None:
        """Row counts must match exactly, or a LATERAL pairs answers with the wrong input rows."""
        result = _call(mock, ["My package never arrived", "Thanks for the quick refund"])
        assert result.schema.names == NOUL_SCHEMA.names
        assert result.schema.types == NOUL_SCHEMA.types
        yes, no = result.column("noul").to_pylist()
        assert yes > 0.5 > no, "the mock answers on keyword overlap; these two must land either side"

    def test_the_answer_is_a_probability_and_nothing_else(self, mock: MockTypeSafeServer) -> None:
        """A noul has no confidence and no distribution; exposing an empty one would be a lie."""
        assert "confidence" not in NOUL_SCHEMA.names
        assert "probabilities" not in NOUL_SCHEMA.names
        (row,) = _call(mock, ["My package never arrived"]).to_pylist()
        assert 0.0 <= row["noul"] <= 1.0
        assert row["model"] == "jev-1.13.0"
        assert row["input_tokens"] > 0 and row["output_tokens"] == 1

    def test_a_null_state_is_a_null_row_and_no_request(self, mock: MockTypeSafeServer) -> None:
        """Row counts must match 1:1 or a LATERAL pairs outputs with the wrong inputs."""
        result = _call(mock, [None, "My package never arrived", None])
        noul = result.column("noul").to_pylist()
        assert noul[0] is None and noul[2] is None and noul[1] is not None
        assert len(mock.requests) == 1

    def test_an_all_null_batch_needs_no_key(self, mock: MockTypeSafeServer) -> None:
        """Nothing is asked, so nothing should require credentials — this must not fail the query."""
        result = _call(mock, [None, None], api_key=None)
        assert result.column("noul").to_pylist() == [None, None]
        assert mock.requests == []

    def test_the_question_reaches_the_api_verbatim(self, mock: MockTypeSafeServer) -> None:
        """A silent rewrite here would change the model's answer with no visible cause."""
        criteria = [("true", "needs a reply today"), ("false", "can wait")]
        _call(
            mock,
            ["hello"],
            arguments=_arguments(model=pa.scalar("jev-1.13"), criteria=pa.scalar(criteria, type=CRITERIA_TYPE)),
        )
        (request,) = mock.requests
        assert request == {
            "state": "hello",
            "model": "jev-1.13",
            "questions": {
                "noul": {"type": "noul", "instructions": INSTRUCTIONS, "criteria": dict(criteria)},
            },
        }

    def test_criteria_are_omitted_from_the_request_when_not_given(self, mock: MockTypeSafeServer) -> None:
        """An empty criteria object is not the same request as no criteria at all."""
        _call(mock, ["hello"])
        (request,) = mock.requests
        assert "criteria" not in request["questions"]["noul"]

    def test_repeated_states_are_billed_once(self, mock: MockTypeSafeServer) -> None:
        """A LATERAL over a low-cardinality column repeats states; each distinct one is paid for once."""
        result = _call(mock, ["lost package"] * 5 + ["invoice"] * 5)
        assert result.num_rows == 10
        assert len(mock.requests) == 2


class TestErrorsSurface:
    """Failures must reach the user as errors, never as quiet NULLs."""

    def test_a_rejected_key_is_an_error_not_a_null(self, mock: MockTypeSafeServer) -> None:
        """A NULL here is indistinguishable from a NULL input, so the query would look like it worked."""
        with pytest.raises(ClientError, match="rejected the API key"):
            _call(mock, ["hello"], api_key="wrong-key")

    def test_missing_instructions_fail_at_bind(self, mock: MockTypeSafeServer) -> None:
        """Caught at plan time, before a single row is billed."""
        with pytest.raises(ClientError, match="requires 'instructions'"):
            _call(mock, ["hello"], arguments=_arguments(instructions=None))
        assert mock.requests == []

    def test_an_unknown_criteria_key_fails_at_bind(self, mock: MockTypeSafeServer) -> None:
        """The API would reject it with a 422 after the row was already on the wire."""
        stray = pa.scalar([("maybe", "who knows")], type=CRITERIA_TYPE)
        with pytest.raises(ClientError, match="'true' and 'false'"):
            _call(mock, ["hello"], arguments=_arguments(criteria=stray))
        assert mock.requests == []


class TestCriteriaOf:
    """Normalising the optional `criteria` argument, which DuckDB may deliver several ways."""

    def test_accepts_map_pairs_and_dicts(self) -> None:
        """An Arrow MAP scalar converts to pairs; a dict is what Python callers pass. Both are valid."""
        assert criteria_of([("true", "x"), ("false", "y")]) == {"true": "x", "false": "y"}
        assert criteria_of([{"key": "true", "value": "x"}]) == {"true": "x"}
        assert criteria_of({"false": "y"}) == {"false": "y"}

    @pytest.mark.parametrize("raw", [None, [], {}])
    def test_absent_criteria_are_not_an_error(self, raw: Any) -> None:
        """Unlike a choice's, a noul's criteria are optional — the instructions alone are a question."""
        assert criteria_of(raw) is None

    def test_one_outcome_alone_is_enough(self) -> None:
        """Describing only what a yes looks like is a legitimate rubric, and the API accepts it."""
        assert criteria_of({"true": "needs a reply today"}) == {"true": "needs a reply today"}

    @pytest.mark.parametrize("key", ["maybe", "TRUE", "yes", ""])
    def test_any_other_key_is_rejected(self, key: str) -> None:
        """A noul has two outcomes; a third key means the caller expected a choice question."""
        with pytest.raises(ValueError, match="'true' and 'false'"):
            criteria_of({key: "x"})


class TestQuestionOf:
    """Assembling the request, which both bind and process go through."""

    def test_a_blank_question_says_what_to_write(self) -> None:
        """The API's 422 does not tell a SQL user which argument to add."""
        with pytest.raises(ValueError, match="instructions =>"):
            question_of("   ", None)

    def test_empty_criteria_leave_the_key_off_the_request(self) -> None:
        """An empty object and an absent one are different requests; only one of them is what we mean."""
        assert question_of("Is this urgent?", {}) == {"type": "noul", "instructions": "Is this urgent?"}


def test_the_function_can_be_used_under_lateral() -> None:
    """DuckDB rejects correlated LATERAL on a function that registers a finalize."""
    assert NoulFunction.has_finalize_override() is False
