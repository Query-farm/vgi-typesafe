# Copyright 2026 Query Farm LLC - https://query.farm

"""``score()`` driven over the real VGI protocol, against the mock endpoint.

This spawns the worker as a subprocess and talks to it with the framework's own
client — everything DuckDB would exercise except DuckDB itself, so it runs
without the C++ extension. ``test_end_to_end.py`` covers the SQL surface.

What is specific to a score, and so worth guarding here, is that its criteria
are an *ordered* list rather than a named set: the position of a level is its
meaning, the answer is expressed in those positions, and a scale with fewer than
two rungs cannot place anything.
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
from vgi_typesafe.score import SCORE_SCHEMA, ScoreFunction, levels_of, question_of

WORKER = [sys.executable, "-m", "vgi_typesafe.worker"]
INSTRUCTIONS = "How severe is this?"
LEVELS = ["minor question", "disruptive delay", "critical outage"]
LEVELS_TYPE = pa.list_(pa.string())


@pytest.fixture
def mock() -> Iterator[MockTypeSafeServer]:
    """A mock TypeSafe endpoint on a free port, requiring a known key."""
    with running(api_key="test-key") as server:
        yield server


def _arguments(**overrides: Any) -> Arguments:
    named = {
        "instructions": pa.scalar(INSTRUCTIONS),
        "criteria": pa.scalar(LEVELS, type=LEVELS_TYPE),
        **overrides,
    }
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
                function_name="score",
                schema_path=["main"],
                input=iter([batch]),
                arguments=arguments or _arguments(),
                secrets=secrets,
                has_finalize=False,
            )
        )
    return pa.Table.from_batches(batches)


class TestScore:
    """The 1->1 contract and the columns a caller reads."""

    def test_one_output_row_per_input_row_in_order(self, mock: MockTypeSafeServer) -> None:
        """Row counts must match exactly, or a LATERAL pairs answers with the wrong input rows."""
        result = _call(mock, ["A minor question", "A critical outage", "A disruptive delay"])
        assert result.schema.names == SCORE_SCHEMA.names
        assert result.schema.types == SCORE_SCHEMA.types
        assert result.num_rows == 3

    def test_the_scale_is_ordered_so_the_scores_are_comparable(self, mock: MockTypeSafeServer) -> None:
        """The whole point of a score over a choice: a worse row gets a bigger number."""
        low, high = _call(mock, ["A minor question", "A critical outage"]).column("score").to_pylist()
        assert low < high

    def test_every_column_is_populated(self, mock: MockTypeSafeServer) -> None:
        """A column that is always NULL is a column nobody can use."""
        (row,) = _call(mock, ["A critical outage"]).to_pylist()
        assert 0.0 <= row["score"] <= len(LEVELS) - 1
        assert 0.0 < row["confidence"] <= 1.0
        assert [level for level, _ in row["probabilities"]] == [0, 1, 2]
        assert sum(p for _, p in row["probabilities"]) == pytest.approx(1.0, abs=1e-5)
        assert row["model"] == "jev-1.13.0"
        assert row["input_tokens"] > 0 and row["output_tokens"] == 1

    def test_probabilities_are_keyed_by_level_number(self, mock: MockTypeSafeServer) -> None:
        """Level text may repeat or be rewritten; the number is the scale, so it is the key.

        The API sends these keys as strings, so an unconverted key would make
        the MAP fail its declared MAP(INTEGER, DOUBLE) type rather than degrade.
        """
        assert SCORE_SCHEMA.field("probabilities").type == pa.map_(pa.int32(), pa.float64())
        (row,) = _call(mock, ["A critical outage"]).to_pylist()
        assert all(isinstance(level, int) for level, _ in row["probabilities"])

    def test_a_null_state_is_a_null_row_and_no_request(self, mock: MockTypeSafeServer) -> None:
        """Row counts must match 1:1 or a LATERAL pairs outputs with the wrong inputs."""
        result = _call(mock, [None, "A critical outage", None])
        assert result.column("score").to_pylist()[0] is None
        assert result.column("probabilities").to_pylist()[2] is None
        assert len(mock.requests) == 1

    def test_an_all_null_batch_needs_no_key(self, mock: MockTypeSafeServer) -> None:
        """Nothing is asked, so nothing should require credentials — this must not fail the query."""
        result = _call(mock, [None, None], api_key=None)
        assert result.column("score").to_pylist() == [None, None]
        assert mock.requests == []

    def test_the_question_reaches_the_api_verbatim_and_in_order(self, mock: MockTypeSafeServer) -> None:
        """Reordering the scale silently would change every score on every row."""
        _call(mock, ["hello"], arguments=_arguments(model=pa.scalar("jev-1.13")))
        (request,) = mock.requests
        assert request == {
            "state": "hello",
            "model": "jev-1.13",
            "questions": {"score": {"type": "score", "instructions": INSTRUCTIONS, "criteria": LEVELS}},
        }

    def test_repeated_states_are_billed_once(self, mock: MockTypeSafeServer) -> None:
        """A LATERAL over a low-cardinality column repeats states; each distinct one is paid for once."""
        result = _call(mock, ["outage"] * 5 + ["question"] * 5)
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

    def test_a_missing_scale_fails_at_bind(self, mock: MockTypeSafeServer) -> None:
        """Caught at plan time, before a single row is billed."""
        with pytest.raises(ClientError, match="requires 'criteria'"):
            _call(mock, ["hello"], arguments=_arguments(criteria=None))
        assert mock.requests == []

    def test_a_one_level_scale_fails_at_bind(self, mock: MockTypeSafeServer) -> None:
        """Production accepts it and scores every row 0.0; we refuse before paying for that."""
        one = pa.scalar(["only one level"], type=LEVELS_TYPE)
        with pytest.raises(ClientError, match="lowest first"):
            _call(mock, ["hello"], arguments=_arguments(criteria=one))
        assert mock.requests == []


class TestLevelsOf:
    """Normalising the ordered `criteria` argument, which is a score's whole scale."""

    def test_order_is_preserved(self) -> None:
        """Level order is the scale; reordering it would renumber every answer."""
        assert levels_of(["c", "b", "a"]) == ["c", "b", "a"]

    @pytest.mark.parametrize("raw", [None, [], ["one"], [f"l{i}" for i in range(11)]])
    def test_a_scale_that_cannot_place_anything_is_rejected(self, raw: Any) -> None:
        """Fewer than two rungs has one possible answer; more than ten is refused upstream."""
        with pytest.raises(ValueError, match="requires 'criteria'"):
            levels_of(raw)

    def test_a_blank_level_is_rejected(self) -> None:
        """The description is all the model has to tell one rung from the next."""
        with pytest.raises(ValueError, match="level 1 is NULL or blank"):
            levels_of(["minor", "   "])


class TestQuestionOf:
    """Assembling the request, which both bind and process go through."""

    def test_a_blank_question_says_what_to_write(self) -> None:
        """The API's 422 does not tell a SQL user which argument to add."""
        with pytest.raises(ValueError, match="instructions =>"):
            question_of("   ", LEVELS)

    def test_the_scale_travels_as_an_ordered_array(self) -> None:
        """A score's criteria is a list on the wire; a map would lose the ordering that defines it."""
        assert question_of(INSTRUCTIONS, LEVELS)["criteria"] == LEVELS


def test_the_function_can_be_used_under_lateral() -> None:
    """DuckDB rejects correlated LATERAL on a function that registers a finalize."""
    assert ScoreFunction.has_finalize_override() is False
