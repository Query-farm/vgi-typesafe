# Copyright 2026 Query Farm LLC - https://query.farm

"""``choice()`` driven over the real VGI protocol, against the mock endpoint.

This spawns the worker as a subprocess and talks to it with the framework's own
client — everything DuckDB would exercise except DuckDB itself, so it runs
without the C++ extension. ``test_end_to_end.py`` covers the SQL surface.
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

from vgi_typesafe.choice import CHOICE_SCHEMA, ChoiceFunction, criteria_of
from vgi_typesafe.mock_server import MockTypeSafeServer, running

WORKER = [sys.executable, "-m", "vgi_typesafe.worker"]
CRITERIA = [
    ("shipping", "Delivery status, delays, lost packages"),
    ("billing", "Charges, invoices, payments"),
]
CRITERIA_TYPE = pa.map_(pa.string(), pa.string())


@pytest.fixture
def mock() -> Iterator[MockTypeSafeServer]:
    with running(api_key="test-key") as server:
        yield server


def _arguments(**overrides: Any) -> Arguments:
    named = {
        "instructions": pa.scalar("Which team should handle this?"),
        "criteria": pa.scalar(CRITERIA, type=CRITERIA_TYPE),
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
                function_name="choice",
                schema_path=["main"],
                input=iter([batch]),
                arguments=arguments or _arguments(),
                secrets=secrets,
                has_finalize=False,
            )
        )
    return pa.Table.from_batches(batches)


class TestChoice:
    def test_one_output_row_per_input_row_in_order(self, mock: MockTypeSafeServer) -> None:
        result = _call(mock, ["My package never arrived", "I was charged twice", "Where is my delivery?"])
        assert result.column("choice").to_pylist() == ["shipping", "billing", "shipping"]
        assert result.schema.names == CHOICE_SCHEMA.names
        assert result.schema.types == CHOICE_SCHEMA.types

    def test_every_column_is_populated(self, mock: MockTypeSafeServer) -> None:
        (row,) = _call(mock, ["I was charged twice"]).to_pylist()
        assert row["choice"] == "billing"
        assert 0.0 < row["confidence"] <= 1.0
        assert [option for option, _ in row["probabilities"]] == ["shipping", "billing"]
        assert sum(p for _, p in row["probabilities"]) == pytest.approx(1.0, abs=1e-5)
        assert row["model"] == "jev-latest"
        assert row["input_tokens"] > 0 and row["output_tokens"] == 1

    def test_a_null_state_is_a_null_row_and_no_request(self, mock: MockTypeSafeServer) -> None:
        """Row counts must match 1:1 or a LATERAL pairs outputs with the wrong inputs."""
        result = _call(mock, [None, "I was charged twice", None])
        assert result.column("choice").to_pylist() == [None, "billing", None]
        assert result.column("probabilities").to_pylist()[0] is None
        assert len(mock.requests) == 1

    def test_an_all_null_batch_needs_no_key(self, mock: MockTypeSafeServer) -> None:
        result = _call(mock, [None, None], api_key=None)
        assert result.column("choice").to_pylist() == [None, None]
        assert mock.requests == []

    def test_the_question_reaches_the_api_verbatim(self, mock: MockTypeSafeServer) -> None:
        _call(mock, ["hello"], arguments=_arguments(model=pa.scalar("jev-1.13")))
        (request,) = mock.requests
        assert request == {
            "state": "hello",
            "model": "jev-1.13",
            "questions": {
                "choice": {
                    "type": "choice",
                    "instructions": "Which team should handle this?",
                    "criteria": dict(CRITERIA),
                }
            },
        }

    def test_repeated_states_are_billed_once(self, mock: MockTypeSafeServer) -> None:
        result = _call(mock, ["lost package"] * 5 + ["invoice"] * 5)
        assert result.num_rows == 10
        assert len(mock.requests) == 2


class TestErrorsSurface:
    def test_a_rejected_key_is_an_error_not_a_null(self, mock: MockTypeSafeServer) -> None:
        with pytest.raises(ClientError, match="rejected the API key"):
            _call(mock, ["hello"], api_key="wrong-key")

    def test_a_missing_key_says_how_to_fix_it(self, mock: MockTypeSafeServer, monkeypatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        with pytest.raises(ClientError, match="CREATE SECRET"):
            _call(mock, ["hello"], api_key=None)

    def test_missing_instructions_fail_at_bind(self, mock: MockTypeSafeServer) -> None:
        with pytest.raises(ClientError, match="requires 'instructions'"):
            _call(mock, ["hello"], arguments=_arguments(instructions=None))
        assert mock.requests == []

    def test_missing_criteria_fail_at_bind(self, mock: MockTypeSafeServer) -> None:
        with pytest.raises(ClientError, match="requires 'criteria'"):
            _call(mock, ["hello"], arguments=_arguments(criteria=None))
        assert mock.requests == []


class TestCriteriaOf:
    def test_accepts_map_pairs_and_dicts(self) -> None:
        assert criteria_of([("a", "x"), ("b", "y")]) == {"a": "x", "b": "y"}
        assert criteria_of([{"key": "a", "value": "x"}]) == {"a": "x"}
        assert criteria_of({"a": "x"}) == {"a": "x"}

    def test_preserves_option_order(self) -> None:
        assert list(criteria_of([("z", ""), ("a", ""), ("m", "")])) == ["z", "a", "m"]

    def test_a_null_description_is_an_empty_one(self) -> None:
        assert criteria_of([("a", None)]) == {"a": ""}

    @pytest.mark.parametrize("raw", [None, [], {}])
    def test_empty_is_rejected(self, raw: Any) -> None:
        with pytest.raises(ValueError, match="requires 'criteria'"):
            criteria_of(raw)

    def test_blank_option_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="blank option"):
            criteria_of([(" ", "x")])

    def test_too_many_options_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="at most 255"):
            criteria_of({f"o{i}": "" for i in range(256)})


def test_the_function_can_be_used_under_lateral() -> None:
    """DuckDB rejects correlated LATERAL on a function that registers a finalize."""
    assert ChoiceFunction.has_finalize_override() is False
