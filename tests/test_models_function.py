# Copyright 2026 Query Farm LLC - https://query.farm

"""``models()`` driven over the real VGI protocol, against the mock endpoint.

The worker runs as a subprocess and is driven with the framework's own client,
so this exercises bind, init and the generator's single tick exactly as DuckDB
would — without needing the C++ extension. ``test_end_to_end.py`` covers the SQL
surface; ``test_typesafe_api.py`` covers the parsing in isolation.

The function is a source, not a transform, so it is driven through
``table_function`` rather than ``table_in_out_function``: there is no input
batch, which is the whole reason it is not a blended function.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pytest
from vgi.client import Client
from vgi.client.client import ClientError

from vgi_typesafe.mock_server import MODELS, MockTypeSafeServer, running
from vgi_typesafe.models import MODELS_SCHEMA, ModelsFunction

WORKER = [sys.executable, "-m", "vgi_typesafe.worker"]


@pytest.fixture
def mock() -> Iterator[MockTypeSafeServer]:
    """A mock TypeSafe endpoint on a free port, requiring a known key."""
    with running(api_key="test-key") as server:
        yield server


def _call(
    mock: MockTypeSafeServer,
    *,
    api_key: str | None = "test-key",
    metadata: list[Any] | None = None,
) -> pa.Table:
    """Scan ``models()`` once, optionally collecting each batch's custom metadata."""
    secrets = {"typesafe": {"api_key": api_key, "base_url": mock.base_url}} if api_key else None
    with Client(WORKER) as client:
        batches = list(
            client.table_function(
                function_name="models",
                schema_path=["main"],
                secrets=secrets,
                batch_metadata_callback=None if metadata is None else metadata.append,
            )
        )
    return pa.Table.from_batches(batches, schema=MODELS_SCHEMA)


class TestTheListing:
    """The rows and columns a caller reads before choosing a `model =>`."""

    def test_every_published_model_is_a_row(self, mock: MockTypeSafeServer) -> None:
        """A listing that silently drops a model is worse than no listing — it reads as complete."""
        result = _call(mock)
        assert result.column("name").to_pylist() == [model["name"] for model in MODELS]

    def test_the_preview_model_is_listed(self, mock: MockTypeSafeServer) -> None:
        """`jev-preview` existing and being undiscoverable from SQL is the reason this function exists."""
        assert "jev-preview" in _call(mock).column("name").to_pylist()

    def test_the_schema_is_exactly_what_is_declared(self, mock: MockTypeSafeServer) -> None:
        """`vgi.result_columns_schema` is a static promise; a drift here makes it a false one."""
        result = _call(mock)
        assert result.schema.names == MODELS_SCHEMA.names
        assert result.schema.types == MODELS_SCHEMA.types

    def test_every_column_is_populated(self, mock: MockTypeSafeServer) -> None:
        """A column that is always NULL is a column nobody can use."""
        row = _call(mock).to_pylist()[0]
        assert row["name"] == "jev-latest"
        assert row["description"].strip()
        assert row["release_date"] == datetime(2026, 9, 10, 18, 38, 1, 391457, tzinfo=UTC)

    def test_a_name_is_usable_as_a_model_argument(self, mock: MockTypeSafeServer) -> None:
        """The listing is only useful if its `name` goes straight back into `model =>` unedited."""
        names = _call(mock).column("name").to_pylist()
        assert all(name == name.strip() and " " not in name for name in names)


class TestCacheability:
    """The listing is advertised as cacheable; that has to survive to the wire."""

    def test_the_ttl_reaches_the_first_batch(self, mock: MockTypeSafeServer) -> None:
        """The client reads `vgi.cache.*` off the first batch only, so it has to be there."""
        seen: list[Any] = []
        _call(mock, metadata=seen)
        assert seen and seen[0] is not None
        tags = dict(zip(seen[0].keys(), seen[0].values(), strict=True))
        assert tags[b"vgi.cache.ttl"] == b"300"
        assert tags[b"vgi.cache.stale_if_error"] == b"3600"


class TestErrorsSurface:
    """Failures must reach the user as errors, never as an empty result set."""

    def test_a_rejected_key_is_an_error_not_an_empty_listing(self, mock: MockTypeSafeServer) -> None:
        """Zero rows would read as "this account has no models", which is a different fact."""
        with pytest.raises(ClientError, match="rejected the API key"):
            _call(mock, api_key="wrong-key")

    def test_a_missing_key_says_how_to_fix_it(self, mock: MockTypeSafeServer, monkeypatch: Any) -> None:
        """Discovery is often the first call a new user makes, so it is the first message they read."""
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        with pytest.raises(ClientError, match="CREATE SECRET"):
            _call(mock, api_key=None)


class TestItIsASourceFunction:
    """The base class this was built on, asserted rather than assumed."""

    def test_the_schema_is_frozen_at_class_level(self) -> None:
        """`@bind_fixed_schema` only applies when nothing overrides on_bind; a later edit could."""
        assert "on_bind" not in ModelsFunction.__dict__ or getattr(
            ModelsFunction.__dict__["on_bind"].__func__, "_is_bind_fixed_schema", False
        )
        assert ModelsFunction.FIXED_SCHEMA is MODELS_SCHEMA
