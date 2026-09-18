# Copyright 2026 Query Farm LLC - https://query.farm

"""``is_true()`` driven over the real VGI protocol, against the mock endpoint.

The scalar is the one function here that is handed an Arrow *vector* and must
decide for itself how many requests that is worth. So the assertions that matter
are about request accounting — one per distinct non-null value, none at all for
a NULL — and about the bind-time check that keeps a blank question from reaching
the API. ``test_end_to_end.py`` covers the SQL surface, including the `WHERE`
clause this function exists for.
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

from vgi_typesafe.is_true import IsTrueFunction, instructions_of, probabilities
from vgi_typesafe.mock_server import MockTypeSafeServer, running

WORKER = [sys.executable, "-m", "vgi_typesafe.worker"]
QUESTION = "Is this message about a lost package?"


@pytest.fixture
def mock() -> Iterator[MockTypeSafeServer]:
    """A mock TypeSafe endpoint on a free port, requiring a known key."""
    with running(api_key="test-key") as server:
        yield server


def _call(
    mock: MockTypeSafeServer,
    states: list[str | None],
    *,
    question: str | None = QUESTION,
    api_key: str | None = "test-key",
) -> list[Any]:
    batch = pa.RecordBatch.from_pydict({"state": pa.array(states, type=pa.string())})
    secrets = {"typesafe": {"api_key": api_key, "base_url": mock.base_url}} if api_key else None
    arguments = Arguments(positional=(pa.scalar(question),))
    with Client(WORKER) as client:
        batches = list(
            client.scalar_function(
                function_name="is_true",
                schema_path=["main"],
                input=iter([batch]),
                arguments=arguments,
                secrets=secrets,
            )
        )
    return pa.Table.from_batches(batches).column(0).to_pylist()


class TestIsTrue:
    """The contract a caller relies on when this appears inside an expression."""

    def test_the_result_is_one_probability_per_row_in_order(self, mock: MockTypeSafeServer) -> None:
        """A scalar's output is positional — a reordered or short result silently mislabels rows."""
        yes, no = _call(mock, ["My package never arrived", "Thanks for the quick refund"])
        assert yes > 0.5 > no, "the mock answers on keyword overlap; these two must land either side"

    def test_the_output_type_is_a_double_so_it_can_be_compared(self, mock: MockTypeSafeServer) -> None:
        """The value is a probability, not a verdict; a BOOLEAN would have thrown the threshold away."""
        assert IsTrueFunction.catalog_output_schema().field(0).type == pa.float64()
        (value,) = _call(mock, ["My package never arrived"])
        assert isinstance(value, float) and 0.0 <= value <= 1.0

    def test_a_null_value_answers_null_and_costs_nothing(self, mock: MockTypeSafeServer) -> None:
        """NULL in, NULL out is the only way this function may produce a NULL."""
        values = _call(mock, [None, "My package never arrived", None])
        assert values[0] is None and values[2] is None and values[1] is not None
        assert len(mock.requests) == 1

    def test_an_all_null_chunk_asks_nothing(self, mock: MockTypeSafeServer) -> None:
        """A chunk with nothing to judge must not spend a request to be told so."""
        assert _call(mock, [None, None]) == [None, None]
        assert mock.requests == []

    def test_one_request_per_distinct_value_not_per_row(self, mock: MockTypeSafeServer) -> None:
        """The reason a scalar over an Arrow vector is affordable at all; per-row would cost 10x here."""
        values = _call(mock, ["lost package"] * 5 + ["an invoice question"] * 5)
        assert len(values) == 10
        assert len(mock.requests) == 2
        assert values[:5] == [values[0]] * 5, "identical inputs must also get identical answers"

    def test_the_question_reaches_the_api_as_a_noul(self, mock: MockTypeSafeServer) -> None:
        """It must be the same question `noul()` sends, or the two would disagree on the same row."""
        _call(mock, ["hello"])
        (request,) = mock.requests
        assert request["questions"] == {"noul": {"type": "noul", "instructions": QUESTION}}
        assert request["model"] == "jev-latest", "a scalar takes no named arguments, so the default applies"


class TestErrorsSurface:
    """Failures must reach the user as errors, never as quiet NULLs."""

    def test_a_rejected_key_is_an_error_not_a_null(self, mock: MockTypeSafeServer) -> None:
        """Inside a WHERE clause a NULL would silently drop the row instead of failing the query."""
        with pytest.raises(ClientError, match="rejected the API key"):
            _call(mock, ["hello"], api_key="wrong-key")

    @pytest.mark.parametrize("question", [None, "", "   "])
    def test_a_blank_question_fails_at_bind(self, mock: MockTypeSafeServer, question: str | None) -> None:
        """Caught at plan time, before a single row is billed."""
        with pytest.raises(ClientError, match="needs a question"):
            _call(mock, ["hello"], question=question)
        assert mock.requests == []


class TestProbabilities:
    """The batching helper on its own, where the protocol cannot hide what it did."""

    def test_an_all_null_vector_resolves_no_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Nothing is asked, so nothing should need a key — otherwise a NULL column fails the query."""
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        empty = pa.array([None, None], type=pa.string())
        assert probabilities(empty, instructions=QUESTION, secret=None).to_pylist() == [None, None]

    def test_an_empty_vector_is_answered_without_a_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DuckDB can hand a scalar a zero-row chunk; asking the API about nothing would be absurd."""
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        assert probabilities(pa.array([], type=pa.string()), instructions=QUESTION, secret=None).to_pylist() == []


class TestInstructionsOf:
    """The bind-time check, isolated from the protocol."""

    def test_the_message_says_what_to_write(self) -> None:
        """The API's 422 does not tell a SQL user that the second argument is the question."""
        with pytest.raises(ValueError, match="second argument"):
            instructions_of(None)

    def test_the_question_is_passed_through_untouched(self) -> None:
        """A silent rewrite would change the model's answer with no visible cause."""
        assert instructions_of("  Is this spam?  ") == "  Is this spam?  "


def test_a_scalar_is_registered_as_consistent() -> None:
    """DuckDB may constant-fold and cache a CONSISTENT scalar; the same state must not be re-billed.

    It is also the honest label: the same content and the same question go to the
    same model, and nothing in this worker adds per-row variation.
    """
    from vgi.metadata import FunctionStability, resolve_metadata

    assert resolve_metadata(IsTrueFunction).stability is FunctionStability.CONSISTENT
