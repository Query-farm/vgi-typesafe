"""SQL executed against a real ATTACH, with the mock endpoint behind it.

This is the only place the things the task is actually about get exercised
together: a correlated LATERAL, the MAP-typed named argument, and a key arriving
through ``CREATE SECRET``.

It needs a DuckDB whose ``vgi`` extension speaks the same protocol as the local
``vgi-python`` checkout. The community build lags it, so this drives the
``haybarn`` shell built from ``../vgi``; point ``HAYBARN`` elsewhere to override.
Hermetic, so it runs by default — and skips when no binary is found.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from vgi_typesafe.mock_server import MockTypeSafeServer, running

pytestmark = pytest.mark.e2e

PROJECT = Path(__file__).resolve().parent.parent
HAYBARN = Path(os.environ.get("HAYBARN") or PROJECT.parent / "vgi" / "build" / "release" / "haybarn")

QUESTION = (
    "instructions => 'Which team should handle this?', "
    "criteria => MAP {'shipping': 'Delivery status, delays, lost packages', "
    "'billing': 'Charges, invoices, payment problems'}"
)


@pytest.fixture
def mock() -> Iterator[MockTypeSafeServer]:
    if not HAYBARN.is_file():
        pytest.skip(f"no haybarn binary at {HAYBARN}; set HAYBARN to a DuckDB shell with the vgi extension")
    with running(api_key="test-key") as server:
        yield server


def _run(
    mock: MockTypeSafeServer, sql: str, *, api_key: str | None = "test-key"
) -> subprocess.CompletedProcess:
    secret = (
        f"CREATE SECRET ts (TYPE typesafe, api_key '{api_key}', base_url '{mock.base_url}');"
        if api_key
        else ""
    )
    # Setup output is discarded so stdout carries exactly one JSON array: the query's.
    script = (
        ".output /dev/null\n"
        f"ATTACH 'typesafe' (TYPE vgi, LOCATION 'uv run typesafe_worker.py');\n{secret}\n"
        f".output\n.mode json\n{sql}"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("TYPESAFE_")}
    return subprocess.run(
        [str(HAYBARN), "-unsigned"],
        input=script,
        capture_output=True,
        text=True,
        timeout=180,
        cwd=PROJECT,
        env=env,
    )


def _rows(mock: MockTypeSafeServer, sql: str) -> list[dict[str, Any]]:
    result = _run(mock, sql)
    assert result.returncode == 0 and not result.stderr.strip(), result.stderr
    return json.loads(result.stdout.strip() or "[]")


def test_a_literal_call(mock: MockTypeSafeServer) -> None:
    (row,) = _rows(mock, f"SELECT * FROM typesafe.main.choice('I was charged twice', {QUESTION});")
    assert row["choice"] == "billing"
    assert set(row["probabilities"]) == {"shipping", "billing"}
    assert row["model"] == "jev-latest"


def test_a_correlated_lateral_pairs_each_row_with_its_own_answer(mock: MockTypeSafeServer) -> None:
    rows = _rows(
        mock,
        f"""
        SELECT t.id, c.choice
        FROM (VALUES (1, 'My package never arrived'), (2, 'I was charged twice'),
                     (3, NULL), (4, 'My package never arrived')) t(id, body),
             LATERAL typesafe.main.choice(t.body, {QUESTION}) c
        ORDER BY t.id;
        """,
    )
    assert rows == [
        {"id": 1, "choice": "shipping"},
        {"id": 2, "choice": "billing"},
        {"id": 3, "choice": None},
        {"id": 4, "choice": "shipping"},
    ]
    assert len(mock.requests) == 2, "the NULL row and the repeated state make no request"


def test_the_implicit_lateral_form(mock: MockTypeSafeServer) -> None:
    rows = _rows(
        mock,
        f"""
        SELECT c.choice, count(*) AS n
        FROM (VALUES ('lost package'), ('late delivery'), ('wrong invoice')) t(body),
             typesafe.main.choice(t.body, {QUESTION}) c
        GROUP BY c.choice ORDER BY c.choice;
        """,
    )
    assert rows == [{"choice": "billing", "n": 1}, {"choice": "shipping", "n": 2}]


def test_the_key_is_redacted_in_duckdb_secrets(mock: MockTypeSafeServer) -> None:
    (row,) = _rows(mock, "SELECT secret_string FROM duckdb_secrets() WHERE type = 'typesafe';")
    assert "test-key" not in row["secret_string"]
    assert "redacted" in row["secret_string"].lower()


def test_a_wrong_key_is_a_query_error(mock: MockTypeSafeServer) -> None:
    result = _run(mock, f"SELECT * FROM typesafe.main.choice('hello', {QUESTION});", api_key="wrong")
    assert "rejected the API key" in result.stderr + result.stdout


def test_no_key_at_all_says_how_to_fix_it(mock: MockTypeSafeServer) -> None:
    result = _run(mock, f"SELECT * FROM typesafe.main.choice('hello', {QUESTION});", api_key=None)
    assert "CREATE SECRET" in result.stderr + result.stdout
    assert mock.requests == []
