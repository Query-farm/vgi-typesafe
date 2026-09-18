# Copyright 2026 Query Farm LLC - https://query.farm

"""SQL executed against a real ATTACH, with the mock endpoint behind it.

This is the only place the things the task is actually about get exercised
together: a correlated LATERAL, struct- and MAP-typed named arguments, native
STRUCT / whole-row state, and a key arriving through ``CREATE SECRET``.

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
    """A mock TypeSafe endpoint on a free port, requiring a known key."""
    if not HAYBARN.is_file():
        pytest.skip(f"no haybarn binary at {HAYBARN}; set HAYBARN to a DuckDB shell with the vgi extension")
    with running(api_key="test-key") as server:
        yield server


def _run(mock: MockTypeSafeServer, sql: str, *, api_key: str | None = "test-key") -> subprocess.CompletedProcess:
    secret = f"CREATE SECRET ts (TYPE typesafe, api_key '{api_key}', base_url '{mock.base_url}');" if api_key else ""
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
    """The simplest call shape a user will try first."""
    (row,) = _rows(mock, f"SELECT * FROM typesafe.main.choice('I was charged twice', {QUESTION});")
    assert row["choice"] == "billing"
    assert set(row["probabilities"]) == {"shipping", "billing"}
    assert row["model"] == "jev-latest"


def test_a_correlated_lateral_pairs_each_row_with_its_own_answer(mock: MockTypeSafeServer) -> None:
    """The join shape this worker exists for; wrong pairing here is silent and wrong."""
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
    """DuckDB allows LATERAL to be omitted; both must behave alike."""
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
    """The secret is visible to anyone on the connection."""
    (row,) = _rows(mock, "SELECT secret_string FROM duckdb_secrets() WHERE type = 'typesafe';")
    assert "test-key" not in row["secret_string"]
    assert "redacted" in row["secret_string"].lower()


def test_a_wrong_key_is_a_query_error(mock: MockTypeSafeServer) -> None:
    """It must fail the query rather than return empty results."""
    result = _run(mock, f"SELECT * FROM typesafe.main.choice('hello', {QUESTION});", api_key="wrong")
    assert "rejected the API key" in result.stderr + result.stdout


def test_no_key_at_all_says_how_to_fix_it(mock: MockTypeSafeServer) -> None:
    """The message is the only guidance a SQL user gets."""
    result = _run(mock, f"SELECT * FROM typesafe.main.choice('hello', {QUESTION});", api_key=None)
    assert "CREATE SECRET" in result.stderr + result.stdout
    assert mock.requests == []


# ---------------------------------------------------------------------------
# ask(): several questions per row
# ---------------------------------------------------------------------------

ASK_QUESTIONS = """questions => {
    'dept':     {'type': 'choice', 'instructions': 'Which team should handle this?',
                 'criteria': {'shipping': 'Delivery status, delays, lost packages',
                              'billing': 'Charges, invoices, payment problems'}},
    'angry':    {'type': 'noul', 'instructions': 'Is the customer angry?',
                 'criteria': {'true': 'furious, unacceptable', 'false': 'calm, thanks'}},
    'severity': {'type': 'score', 'instructions': 'How bad is it?',
                 'criteria': ['minor question', 'disruptive delay', 'critical outage']}}"""

TICKETS = """(VALUES (1, 'My package is lost, this is unacceptable', 'gold'),
                     (2, 'Thanks, one invoice question', 'free'),
                     (3, NULL, NULL)) t(id, body, tier)"""


def test_ask_answers_every_question_from_one_request_per_row(mock: MockTypeSafeServer) -> None:
    """The economics of ask(), proven through real SQL."""
    rows = _rows(
        mock,
        f"""
        SELECT t.id, a.dept.choice AS dept, a.angry.noul > 0.5 AS angry,
               round(a.severity.score) AS severity, a.usage.output_tokens AS answered
        FROM {TICKETS}, LATERAL typesafe.main.ask({{'message': t.body, 'tier': t.tier}}, {ASK_QUESTIONS}) a
        ORDER BY t.id;
        """,
    )
    assert rows == [
        {"id": 1, "dept": "shipping", "angry": True, "severity": 1.0, "answered": 3},
        {"id": 2, "dept": "billing", "angry": False, "severity": 0.0, "answered": 3},
        {"id": 3, "dept": None, "angry": None, "severity": None, "answered": None},
    ]
    assert len(mock.requests) == 2, "a row whose struct holds only NULLs makes no request"
    assert mock.requests[0]["state"].keys() == {"message", "tier"}


def test_ask_takes_the_whole_row_as_its_state(mock: MockTypeSafeServer) -> None:
    """Needs an ANY-typed blended input column, which the extension only recently supports."""
    rows = _rows(
        mock,
        f"SELECT a.dept.choice AS dept FROM {TICKETS}, LATERAL typesafe.main.ask(t, {ASK_QUESTIONS}) a WHERE t.id = 1;",
    )
    assert rows == [{"dept": "shipping"}]
    assert {"id": 1, "tier": "gold"}.items() <= mock.requests[0]["state"].items()


def test_ask_output_columns_are_typed_per_question(mock: MockTypeSafeServer) -> None:
    """What DESCRIBE shows is the contract a caller plans against."""
    rows = _rows(mock, f"DESCRIBE SELECT * FROM typesafe.main.ask('x', {ASK_QUESTIONS});")
    assert {r["column_name"]: r["column_type"] for r in rows} == {
        "dept": "STRUCT(choice VARCHAR, confidence DOUBLE, probabilities MAP(VARCHAR, DOUBLE))",
        "angry": "STRUCT(noul DOUBLE)",
        "severity": "STRUCT(score DOUBLE, confidence DOUBLE, probabilities MAP(INTEGER, DOUBLE))",
        "usage": "STRUCT(model VARCHAR, input_tokens BIGINT, output_tokens BIGINT)",
    }


def test_ask_accepts_questions_as_json_and_a_json_state(mock: MockTypeSafeServer) -> None:
    """The all-JSON path, end to end."""
    rows = _rows(
        mock,
        """
        SELECT a.spam.noul < 0.5 AS not_spam
        FROM (VALUES ('{"subject": "lunch?", "body": "see you at noon"}'::JSON)) t(payload),
             LATERAL typesafe.main.ask(t.payload, parse_json => true, questions =>
                 '{"spam": {"type": "noul", "instructions": "Is this a lottery prize scam?"}}') a;
        """,
    )
    assert rows == [{"not_spam": True}]
    assert mock.requests[0]["state"] == {"subject": "lunch?", "body": "see you at noon"}


def test_ask_pairs_rows_across_many_input_batches(mock: MockTypeSafeServer) -> None:
    """One batch can hide a pairing bug; several thousand rows cannot."""
    rows = _rows(
        mock,
        """
        WITH t AS (SELECT i AS id, CASE WHEN i % 3 = 0 THEN 'invoice charges' ELSE 'lost package' END AS body,
                          CASE WHEN i % 3 = 0 THEN 'billing' ELSE 'shipping' END AS expected
                   FROM range(3000) r(i))
        SELECT count(*) AS n, count(*) FILTER (WHERE a.dept.choice = t.expected) AS correct
        FROM t, LATERAL typesafe.main.ask({'id': t.id, 'message': t.body},
            questions => {'dept': {'type': 'choice', 'instructions': 'Which team?',
                                   'criteria': {'shipping': 'lost packages', 'billing': 'invoice charges'}}},
            concurrency => 16) a;
        """,
    )
    assert rows == [{"n": 3000, "correct": 3000}]


def test_ask_rejects_a_malformed_question_at_bind(mock: MockTypeSafeServer) -> None:
    """Plan-time rejection, before anything is billed."""
    result = _run(
        mock,
        "SELECT * FROM typesafe.main.ask('x', questions => "
        "{'sev': {'type': 'score', 'instructions': 'How bad?', 'criteria': ['only one level']}});",
    )
    assert "score question 'sev' requires 'criteria'" in result.stderr + result.stdout
    assert mock.requests == []


def test_ask_rejects_a_bare_scalar_state_with_a_way_out(mock: MockTypeSafeServer) -> None:
    """The error must say what to pass instead."""
    result = _run(mock, f"SELECT * FROM typesafe.main.ask(42, {ASK_QUESTIONS});")
    assert "state must be VARCHAR, STRUCT, LIST or MAP" in result.stderr + result.stdout


# ---------------------------------------------------------------------------
# documentation is executable
# ---------------------------------------------------------------------------


def _published_examples() -> list[tuple[str, str]]:
    from vgi_typesafe.ask import AskFunction
    from vgi_typesafe.choice import ChoiceFunction

    return [
        (f"{function.Meta.name}: {example.description}", example.sql)
        for function in (AskFunction, ChoiceFunction)
        for example in function.Meta.examples
    ]


@pytest.mark.parametrize(("label", "sql"), _published_examples(), ids=[label for label, _ in _published_examples()])
def test_every_published_example_runs(mock: MockTypeSafeServer, label: str, sql: str) -> None:
    """The examples in the catalog metadata are what users and agents copy; they must work."""
    assert _rows(mock, f"{sql};"), label


def test_the_readme_headline_query_runs(mock: MockTypeSafeServer) -> None:
    """The first thing a reader copies; it has to work."""
    readme = (PROJECT / "README.md").read_text()
    query = readme.split("-- Route, flag and grade every ticket", 1)[1].split("```", 1)[0]
    query = query.split("\n", 1)[1]  # drop the rest of the comment line
    rows = _rows(
        mock,
        "CREATE TEMP TABLE tickets AS SELECT * FROM (VALUES "
        "(1, 'My package is lost and delivery is delayed'), "
        "(2, 'I was charged twice on my invoice')) t(id, body);\n"
        + query.replace("WHERE a.dept.confidence > 0.8", "ORDER BY t.id"),
    )
    assert [r["choice"] for r in rows] == ["shipping", "billing"]
