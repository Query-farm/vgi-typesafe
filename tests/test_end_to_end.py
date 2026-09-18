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
import re
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
    assert row["model"] == "jev-1.13.0"


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
# ask_dynamic(): questions that differ from row to row
# ---------------------------------------------------------------------------

DEPT_JSON = (
    '{"dept": {"type": "choice", "instructions": "Which team should handle this?", '
    '"criteria": {"shipping": "Delivery status, delays, lost packages", '
    '"billing": "Charges, invoices, payment problems"}}}'
)
URGENT_JSON = '{"urgent": {"type": "noul", "instructions": "Does this need a reply today?"}}'


def test_ask_dynamic_asks_each_row_the_questions_that_row_carries(mock: MockTypeSafeServer) -> None:
    """A JSON column of questions is the form the docs recommend, and the reason this function exists.

    Nothing here is expressible with ask(), whose questions are fixed when the
    query is planned — and the extraction is the other half of the contract: the
    answers are only useful if DuckDB's JSON functions can reach into them.
    """
    rows = _rows(
        mock,
        f"""
        SELECT t.id,
               a.answers->'dept'->>'choice' AS dept,
               (a.answers->'urgent'->>'noul')::DOUBLE > 0.5 AS urgent
        FROM (VALUES (1, 'My package is lost and delivery is delayed', '{DEPT_JSON}'),
                     (2, 'Thanks, just one routine question', '{URGENT_JSON}'),
                     (3, NULL, NULL)) t(id, body, questions),
             LATERAL typesafe.main.ask_dynamic(t.body, t.questions) a
        ORDER BY t.id;
        """,
    )
    assert rows == [
        {"id": 1, "dept": "shipping", "urgent": None},
        {"id": 2, "dept": None, "urgent": False},
        {"id": 3, "dept": None, "urgent": None},
    ]
    assert len(mock.requests) == 2, "the NULL row makes no request and never looks at its questions"
    # Requests are issued concurrently, so their order is not the input's.
    assert sorted(tuple(request["questions"]) for request in mock.requests) == [("dept",), ("urgent",)]


def test_ask_dynamic_takes_heterogeneous_questions_from_a_struct_column(mock: MockTypeSafeServer) -> None:
    """The other accepted form, and the one with a trap: DuckDB unifies the column's type across rows.

    Each row therefore arrives carrying the other row's question as NULL. If that
    padding reached the API, every row would be billed for — and answered with —
    a question it never asked.
    """
    rows = _rows(
        mock,
        """
        SELECT t.id, a.answers
        FROM (VALUES (1, 'My package is lost', {'dept': {'type': 'choice', 'instructions': 'Which team?',
                          'criteria': {'shipping': 'Lost packages', 'billing': 'Invoices'}}}),
                     (2, 'Thanks', {'urgent': {'type': 'noul', 'instructions': 'Does this need a reply today?',
                          'criteria': NULL}})) t(id, body, questions),
             LATERAL typesafe.main.ask_dynamic(t.body, t.questions) a
        ORDER BY t.id;
        """,
    )
    assert [sorted(json.loads(row["answers"])) for row in rows] == [["dept"], ["urgent"]]
    # Requests are issued concurrently, so their order is not the input's.
    assert sorted(tuple(request["questions"]) for request in mock.requests) == [("dept",), ("urgent",)]


def test_ask_dynamic_declares_a_fixed_shape_because_the_answers_are_not(mock: MockTypeSafeServer) -> None:
    """What DESCRIBE shows is the whole contract here — the JSON's shape is deliberately not in it."""
    rows = _rows(mock, f"DESCRIBE SELECT * FROM typesafe.main.ask_dynamic('x', '{URGENT_JSON}');")
    assert {r["column_name"]: r["column_type"] for r in rows} == {
        "answers": "VARCHAR",
        "model": "VARCHAR",
        "input_tokens": "BIGINT",
        "output_tokens": "BIGINT",
    }


def test_ask_dynamic_bills_one_request_per_distinct_pair(mock: MockTypeSafeServer) -> None:
    """De-duplicating on the state alone would answer one of these two questions with the other's."""
    rows = _rows(
        mock,
        f"""
        SELECT count(*) AS n, count(DISTINCT a.answers) AS distinct_answers
        FROM (VALUES ('My package is lost', '{DEPT_JSON}'),
                     ('My package is lost', '{DEPT_JSON}'),
                     ('My package is lost', '{URGENT_JSON}')) t(body, questions),
             LATERAL typesafe.main.ask_dynamic(t.body, t.questions) a;
        """,
    )
    assert rows == [{"n": 3, "distinct_answers": 2}]
    assert len(mock.requests) == 2, "one state, two different questions, and one repeat"


def test_ask_dynamic_score_levels_stay_usable_keys_inside_the_json(mock: MockTypeSafeServer) -> None:
    """ask() keys these with MAP(INTEGER, DOUBLE); JSON has only text keys, so they must read back as digits."""
    rows = _rows(
        mock,
        """
        SELECT (a.answers->'sev'->'probabilities'->>'2')::DOUBLE AS worst
        FROM typesafe.main.ask_dynamic('The whole site is down, a critical outage',
            '{"sev": {"type": "score", "instructions": "How severe is this?",
              "criteria": ["minor question", "disruptive delay", "critical outage"]}}') a;
        """,
    )
    assert rows[0]["worst"] > 0.5


def test_ask_dynamic_points_at_the_row_whose_questions_are_missing(mock: MockTypeSafeServer) -> None:
    """Questions are data here, so one row in a scan can be the broken one; the message must locate it.

    Under a correlated LATERAL the batch is one row, so the index says nothing on
    its own — quoting the offending row's own content is what makes the error
    actionable in a table of thousands.
    """
    result = _run(
        mock,
        f"""
        SELECT a.answers FROM (VALUES ('My package is lost', '{DEPT_JSON}'), ('No question for me', NULL))
             t(body, questions), LATERAL typesafe.main.ask_dynamic(t.body, t.questions) a;
        """,
    )
    output = result.stderr + result.stdout
    assert "carries no questions" in output
    assert "No question for me" in output


def test_ask_dynamic_names_the_row_and_the_question_that_is_malformed(mock: MockTypeSafeServer) -> None:
    """ask() catches this at bind; here it can only be caught mid-scan, so it has to say a great deal more."""
    result = _run(
        mock,
        """
        SELECT a.answers FROM typesafe.main.ask_dynamic('x',
            '{"sev": {"type": "score", "instructions": "How bad?", "criteria": ["only one level"]}}') a;
        """,
    )
    assert "ask_dynamic(): score question 'sev' requires 'criteria'" in result.stderr + result.stdout
    assert "row 0 of this input batch" in result.stderr + result.stdout


def test_ask_dynamic_rejects_a_questions_column_that_cannot_hold_one(mock: MockTypeSafeServer) -> None:
    """A column of the wrong type is wrong for every row at once, so it should fail before the scan starts."""
    result = _run(mock, "SELECT * FROM typesafe.main.ask_dynamic('x', 42);")
    assert "questions must be VARCHAR, STRUCT or MAP" in result.stderr + result.stdout
    assert mock.requests == []


# ---------------------------------------------------------------------------
# noul() and score(): the other two one-question shorthands
# ---------------------------------------------------------------------------

LOST_PACKAGE = "instructions => 'Is this message about a lost package?'"
SEVERITY = (
    "instructions => 'How severe is this?', criteria => ['minor question', 'disruptive delay', 'critical outage']"
)


def test_noul_answers_a_probability_per_row_under_lateral(mock: MockTypeSafeServer) -> None:
    """The join shape this worker exists for, on the type whose answer is a bare number."""
    rows = _rows(
        mock,
        f"""
        SELECT t.id, n.noul > 0.5 AS yes
        FROM (VALUES (1, 'My package never arrived'), (2, 'Thanks for the quick refund'),
                     (3, NULL)) t(id, body),
             LATERAL typesafe.main.noul(t.body, {LOST_PACKAGE}) n
        ORDER BY t.id;
        """,
    )
    assert rows == [{"id": 1, "yes": True}, {"id": 2, "yes": False}, {"id": 3, "yes": None}]
    assert len(mock.requests) == 2, "the NULL row makes no request"


def test_noul_declares_no_confidence_column(mock: MockTypeSafeServer) -> None:
    """A noul answer is one number; a confidence column here would have to be invented."""
    rows = _rows(mock, "DESCRIBE SELECT * FROM typesafe.main.noul('x', instructions => 'Is this a test?');")
    assert {r["column_name"]: r["column_type"] for r in rows} == {
        "noul": "DOUBLE",
        "model": "VARCHAR",
        "input_tokens": "BIGINT",
        "output_tokens": "BIGINT",
    }


def test_noul_criteria_describe_the_two_outcomes(mock: MockTypeSafeServer) -> None:
    """The MAP-typed named argument, and the bind check that it names only outcomes."""
    rows = _rows(
        mock,
        "SELECT noul FROM typesafe.main.noul('My package never arrived', "
        "instructions => 'Is this urgent?', "
        "criteria => MAP {'true': 'a lost package', 'false': 'a routine question'});",
    )
    assert rows[0]["noul"] > 0.5
    assert mock.requests[0]["questions"]["noul"]["criteria"] == {
        "true": "a lost package",
        "false": "a routine question",
    }
    result = _run(
        mock,
        "SELECT noul FROM typesafe.main.noul('x', instructions => 'Is this urgent?', "
        "criteria => MAP {'maybe': 'who knows'});",
    )
    assert "'true' and 'false'" in result.stderr + result.stdout


def test_score_places_rows_on_the_scale_in_its_declared_types(mock: MockTypeSafeServer) -> None:
    """A LIST-typed named argument is new here, and the INTEGER-keyed MAP is what DESCRIBE must agree to."""
    described = _rows(mock, f"DESCRIBE SELECT * FROM typesafe.main.score('x', {SEVERITY});")
    assert {r["column_name"]: r["column_type"] for r in described} == {
        "score": "DOUBLE",
        "confidence": "DOUBLE",
        "probabilities": "MAP(INTEGER, DOUBLE)",
        "model": "VARCHAR",
        "input_tokens": "BIGINT",
        "output_tokens": "BIGINT",
    }
    rows = _rows(
        mock,
        f"""
        SELECT t.id, s.score
        FROM (VALUES (1, 'A minor question'), (2, 'A critical outage')) t(id, body),
             LATERAL typesafe.main.score(t.body, {SEVERITY}) s
        ORDER BY t.id;
        """,
    )
    assert rows[0]["score"] < rows[1]["score"], "an ordered scale has to order the rows"
    assert mock.requests[0]["questions"]["score"]["criteria"] == [
        "minor question",
        "disruptive delay",
        "critical outage",
    ]


def test_score_rejects_a_scale_too_short_to_place_anything(mock: MockTypeSafeServer) -> None:
    """Plan-time rejection, before anything is billed — production would accept it and answer 0.0."""
    result = _run(
        mock,
        "SELECT * FROM typesafe.main.score('x', instructions => 'How bad?', criteria => ['only one level']);",
    )
    assert "lowest first" in result.stderr + result.stdout
    assert mock.requests == []


# ---------------------------------------------------------------------------
# is_true(): the scalar form of a noul
# ---------------------------------------------------------------------------


def test_is_true_filters_rows_in_a_where_clause(mock: MockTypeSafeServer) -> None:
    """The entire reason the scalar exists: a judgment inline in WHERE, with no join around it."""
    rows = _rows(
        mock,
        """
        SELECT t.id FROM (VALUES (1, 'My package never arrived'), (2, 'Thanks for the quick refund'),
                                 (3, NULL)) t(id, body)
        WHERE typesafe.main.is_true(t.body, 'Is this message about a lost package?') > 0.5
        ORDER BY t.id;
        """,
    )
    assert rows == [{"id": 1}], "the NULL row is NULL, not true, so WHERE drops it"
    assert len(mock.requests) == 2


def test_is_true_composes_in_case_and_order_by(mock: MockTypeSafeServer) -> None:
    """The other expression positions the docs claim; each would need its own join otherwise."""
    rows = _rows(
        mock,
        """
        SELECT t.id, CASE WHEN typesafe.main.is_true(t.body, 'Is this message about a lost package?') > 0.5
                          THEN 'shipping' ELSE 'other' END AS route
        FROM (VALUES (1, 'Thanks for the quick refund'), (2, 'My package never arrived')) t(id, body)
        ORDER BY typesafe.main.is_true(t.body, 'Is this message about a lost package?') DESC;
        """,
    )
    assert rows == [{"id": 2, "route": "shipping"}, {"id": 1, "route": "other"}]


def test_is_true_agrees_with_the_table_function_on_the_same_row(mock: MockTypeSafeServer) -> None:
    """Two spellings of one question that disagreed would make the shorthand a trap.

    This is also the proof that the scalar is shorter rather than more capable:
    the same filter is expressible either way.
    """
    rows = _rows(
        mock,
        """
        SELECT typesafe.main.is_true('My package never arrived', 'Is this message about a lost package?') AS scalar,
               (SELECT noul FROM typesafe.main.noul('My package never arrived',
                   instructions => 'Is this message about a lost package?')) AS table_function;
        """,
    )
    assert rows[0]["scalar"] == rows[0]["table_function"]


def test_is_true_rejects_a_blank_question_at_bind(mock: MockTypeSafeServer) -> None:
    """Plan-time rejection, before anything is billed."""
    result = _run(mock, "SELECT typesafe.main.is_true('hello', '');")
    assert "needs a question" in result.stderr + result.stdout
    assert mock.requests == []


# ---------------------------------------------------------------------------
# models(): the model listing
# ---------------------------------------------------------------------------


def test_models_lists_every_model_with_its_declared_types(mock: MockTypeSafeServer) -> None:
    """The one function with a fixed, statically declared schema; DESCRIBE is that declaration."""
    rows = _rows(mock, "DESCRIBE SELECT * FROM typesafe.main.models();")
    assert {r["column_name"]: r["column_type"] for r in rows} == {
        "name": "VARCHAR",
        "description": "VARCHAR",
        "release_date": "TIMESTAMP WITH TIME ZONE",
    }


def test_models_returns_the_preview_model_a_user_could_not_otherwise_find(mock: MockTypeSafeServer) -> None:
    """The reason the function exists: `jev-preview` is not mentioned anywhere else in SQL."""
    rows = _rows(mock, "SELECT name, release_date FROM typesafe.main.models() ORDER BY name;")
    assert [r["name"] for r in rows] == ["jev-latest", "jev-preview"]
    assert all(r["release_date"].startswith("2026-09-10") for r in rows)


def test_a_listed_name_is_accepted_as_a_model_argument(mock: MockTypeSafeServer) -> None:
    """Discovery is only worth shipping if what it returns is usable as another function's input."""
    listed = {row["name"] for row in _rows(mock, "SELECT name FROM typesafe.main.models();")}
    assert "jev-preview" in listed
    rows = _rows(mock, f"SELECT choice FROM typesafe.main.choice('lost package', model => 'jev-preview', {QUESTION});")
    assert rows == [{"choice": "shipping"}]
    assert mock.requests[-1]["model"] == "jev-preview", "the listed name must reach the API unchanged"


def test_a_model_column_cannot_be_correlated_into_a_question(mock: MockTypeSafeServer) -> None:
    """Pins the limitation the docs warn about: `model =>` binds once, so it takes a literal.

    Without this, the obvious next thing a reader tries — joining this listing
    into `choice()` — fails with a binder error the docs never mentioned.
    """
    result = _run(
        mock,
        f"SELECT c.choice FROM typesafe.main.models() m, "
        f"LATERAL typesafe.main.choice('lost package', model => m.name, {QUESTION}) c;",
    )
    assert "lateral join column parameters" in result.stderr + result.stdout


def test_models_reads_as_a_table_as_well_as_a_function(mock: MockTypeSafeServer) -> None:
    """One name is registered as both; DuckDB keeps them in separate catalog sets, so both must resolve.

    The table form is what a parameterless listing should look like in SQL, and
    it is what vgi-lint's VGI311 asks for. If the two ever stopped agreeing, one
    of them would be quietly serving something else.
    """
    as_table = _rows(mock, "SELECT name, description FROM typesafe.main.models ORDER BY name;")
    as_function = _rows(mock, "SELECT name, description FROM typesafe.main.models() ORDER BY name;")
    assert as_table == as_function
    assert [r["name"] for r in as_table] == ["jev-latest", "jev-preview"]
    assert _rows(mock, "SELECT table_name FROM duckdb_tables() WHERE database_name = 'typesafe';") == [
        {"table_name": "models"}
    ]


def test_the_models_table_declares_the_row_identity(mock: MockTypeSafeServer) -> None:
    """An agent planning a join needs to know `name` identifies a row; nothing else in the row does."""
    rows = _rows(mock, "DESCRIBE typesafe.main.models;")
    by_name = {r["column_name"]: r for r in rows}
    assert by_name["name"]["key"] == "PRI"
    assert by_name["name"]["null"] == "NO" and by_name["description"]["null"] == "NO"
    assert by_name["release_date"]["null"] == "YES", "the API may omit a release date"


def test_models_takes_no_arguments(mock: MockTypeSafeServer) -> None:
    """A stray accepted argument would be an undocumented, unimplemented knob."""
    result = _run(mock, "SELECT * FROM typesafe.main.models('jev-latest');")
    assert result.returncode != 0 or result.stderr.strip(), "models() should not accept an argument"


def test_models_fails_the_query_rather_than_returning_nothing(mock: MockTypeSafeServer) -> None:
    """Zero rows would read as "this account has no models", which is a different fact entirely."""
    result = _run(mock, "SELECT * FROM typesafe.main.models();", api_key="wrong")
    assert "rejected the API key" in result.stderr + result.stdout


# ---------------------------------------------------------------------------
# documentation is executable
# ---------------------------------------------------------------------------


def _published_examples() -> list[tuple[str, str]]:
    from vgi_typesafe.ask import AskFunction
    from vgi_typesafe.ask_dynamic import AskDynamicFunction
    from vgi_typesafe.choice import ChoiceFunction
    from vgi_typesafe.is_true import IsTrueFunction
    from vgi_typesafe.models import ModelsFunction
    from vgi_typesafe.noul import NoulFunction
    from vgi_typesafe.score import ScoreFunction

    return [
        (f"{function.Meta.name}: {example.description}", example.sql)
        for function in (
            AskFunction,
            AskDynamicFunction,
            ChoiceFunction,
            NoulFunction,
            ScoreFunction,
            IsTrueFunction,
            ModelsFunction,
        )
        for example in function.Meta.examples
    ]


@pytest.mark.parametrize(("label", "sql"), _published_examples(), ids=[label for label, _ in _published_examples()])
def test_every_published_example_runs(mock: MockTypeSafeServer, label: str, sql: str) -> None:
    """The examples in the catalog metadata are what users and agents copy; they must work."""
    assert _rows(mock, f"{sql};"), label


def _readme_walkthrough_sql() -> list[str]:
    """Every ```sql block in the README's walkthrough, in order.

    The walkthrough is what a reader copies before reading anything else, so it is
    the part most worth executing. The ATTACH/secret block is skipped — this
    harness supplies its own, pointed at the mock.

    Returns:
        One SQL script per fenced block worth running.
    """
    readme = (PROJECT / "README.md").read_text()
    walkthrough = readme[readme.index("## Install and attach") : readme.index("## The functions")]
    blocks = re.findall(r"```sql\n(.*?)```", walkthrough, re.S)
    return [b for b in blocks if "ATTACH" not in b and "INSTALL vgi" not in b]


README_SQL = _readme_walkthrough_sql()


@pytest.mark.parametrize("sql", README_SQL, ids=[f"block{i}" for i in range(1, len(README_SQL) + 1)])
def test_every_readme_example_runs(mock: MockTypeSafeServer, sql: str) -> None:
    """The walkthrough is the first thing a reader copies; all of it has to work.

    Executed rather than eyeballed, and keyed on the section headings rather than a
    comment string — an earlier version keyed on a comment, which a reword silently
    broke. Row counts are not asserted: the bundled endpoint scores by keyword
    overlap, so a threshold that selects rows against production may select none
    here. What must hold is that every statement binds and runs.
    """
    setup = (
        "CREATE TABLE tickets AS SELECT * FROM (VALUES "
        "(1, 'My package never arrived and tracking has not updated', 'gold'), "
        "(2, 'I was charged twice on my invoice', 'free')) t(id, body, tier);\n"
    )
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    result = _run(mock, setup + body)
    assert result.returncode == 0 and not result.stderr.strip(), (
        f"a README example failed:\n{sql}\n{result.stderr.strip()}"
    )
