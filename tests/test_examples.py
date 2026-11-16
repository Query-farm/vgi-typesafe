# Copyright 2026 Query Farm LLC - https://query.farm

"""Every example this worker publishes must actually run.

Examples reach a client through five separate carriers, and they are the whole
discovery surface: an agent or a new user copies them before reading anything
else. A broken one is worse than a missing one.

The earlier version of this file only executed the two functions' native
``Meta.examples``. That left four of the five agent-test reference queries
unchecked, and all four selected ``FROM tickets`` — a table this worker does not
have and ``vgi-lint simulate`` never creates, since it attaches only the worker.
They could not run at all. Hence: collect from every carrier, execute all of it.

Execution happens in ``test/sql/examples.test``, which ci/run-integration.sh runs
through DuckDB against the bundled mock over stdio, launch and HTTP. That file is
generated from the carriers below; :func:`test_the_sql_suite_runs_every_published_example`
fails when it drifts. Regenerate it with::

    UPDATE_SQL_TESTS=1 uv run pytest tests/test_examples.py
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from vgi_typesafe import worker as worker_module
from vgi_typesafe.ask import AskFunction
from vgi_typesafe.ask_dynamic import AskDynamicFunction
from vgi_typesafe.choice import ChoiceFunction
from vgi_typesafe.is_true import IsTrueFunction
from vgi_typesafe.models import ModelsFunction
from vgi_typesafe.noul import NoulFunction
from vgi_typesafe.score import ScoreFunction

PROJECT = Path(__file__).resolve().parent.parent
EXAMPLES_TEST = PROJECT / "test" / "sql" / "examples.test"

FUNCTIONS = (
    AskFunction,
    AskDynamicFunction,
    ChoiceFunction,
    NoulFunction,
    ScoreFunction,
    IsTrueFunction,
    ModelsFunction,
)
#: Tables are a carrier too: `models` is exposed as one as well as a function,
#: and its examples use the no-parentheses form, which no function example can.
TABLES = tuple(table for schema in worker_module._TYPESAFE_CATALOG.schemas for table in schema.tables)


def _agent_graders() -> list[dict[str, str]]:
    """The private grader entries, parsed from the YAML without a yaml dependency.

    Only ``name`` and ``reference_sql`` are needed here, and the file's shape is
    fixed, so a tiny reader beats adding a test-only dependency.

    Returns:
        One ``{"name", "reference_sql"}`` mapping per task.
    """
    tasks: list[dict[str, str]] = []
    name: str | None = None
    sql: list[str] = []
    collecting = False
    for raw in (PROJECT / "vgi-agent-tests.yaml").read_text().splitlines():
        line = raw.strip()
        if line.startswith("- name:"):
            if name and sql:
                tasks.append({"name": name, "reference_sql": " ".join(sql)})
            name, sql, collecting = line.split(":", 1)[1].strip(), [], False
        elif line.startswith("reference_sql:"):
            collecting = True
        elif line.startswith(("success_criteria:", "unordered:", "check_sql:")):
            collecting = False
        elif collecting and line:
            sql.append(line)
    if name and sql:
        tasks.append({"name": name, "reference_sql": " ".join(sql)})
    return tasks


def _published_sql() -> list[tuple[str, str]]:
    """Every SQL statement this worker ships, from every carrier that can reach a client.

    Returns:
        ``(label, sql)`` pairs, labelled by carrier so a failure names it.
    """
    found: list[tuple[str, str]] = []
    for function in FUNCTIONS:
        name = function.Meta.name
        for example in function.Meta.examples:
            found.append((f"{name} Meta.examples: {example.description}", example.sql))
        for entry in json.loads(function.Meta.tags["vgi.example_queries"]):
            found.append((f"{name} vgi.example_queries: {entry['description']}", entry["sql"]))
    for table in TABLES:
        for entry in json.loads(table.tags["vgi.example_queries"]):
            found.append((f"{table.name} table vgi.example_queries: {entry['description']}", entry["sql"]))
    for entry in json.loads(worker_module._SCHEMA_TAGS["vgi.example_queries"]):
        found.append((f"schema vgi.example_queries: {entry['description']}", entry["sql"]))
    for entry in json.loads(worker_module._EXECUTABLE_EXAMPLES):
        found.append((f"vgi.executable_examples: {entry['name']}", entry["sql"]))
    for task in _agent_graders():
        found.append((f"agent grader: {task['name']}", task["reference_sql"]))
    return found


PUBLISHED = _published_sql()


def _readme_walkthrough_sql() -> list[str]:
    """Every ```sql block in the README's walkthrough, in order.

    The walkthrough is what a reader copies before reading anything else, so it is
    the part most worth executing. The ATTACH/secret block is skipped — the SQL
    suite supplies its own, pointed at the mock.

    Returns:
        One SQL script per fenced block worth running.
    """
    readme = (PROJECT / "README.md").read_text()
    walkthrough = readme[readme.index("## Install and attach") : readme.index("## The functions")]
    blocks = re.findall(r"```sql\n(.*?)```", walkthrough, re.S)
    return [b for b in blocks if "ATTACH" not in b and "INSTALL vgi" not in b]


def _statements(script: str) -> list[str]:
    """Split a README block into statements: comment lines dropped, split on a line-ending ``;``.

    Returns:
        Each statement without its terminator.
    """
    lines = [line for line in script.splitlines() if not line.strip().startswith("--")]
    text = re.sub(r";[ \t]*--[^\n]*", ";", "\n".join(lines))
    return [part.strip() for part in re.split(r";[ \t]*(?:\n|$)", text) if part.strip()]


_HEADER = """\
# name: test/sql/examples.test
# description: Every published example and README walkthrough statement runs against the mock
# group: [typesafe]
#
# GENERATED by tests/test_examples.py from the worker's example carriers and the
# README; do not edit. Regenerate: UPDATE_SQL_TESTS=1 uv run pytest tests/test_examples.py

require vgi

require-env VGI_TYPESAFE_WORKER

require-env TYPESAFE_MOCK_URL

statement ok
LOAD json;

statement ok
ATTACH 'typesafe' (TYPE vgi, LOCATION '{VGI_TYPESAFE_WORKER}');

statement ok
CREATE SECRET ts (TYPE typesafe, api_key 'test-key', base_url '{TYPESAFE_MOCK_URL}');
"""


def render_examples_test() -> str:
    """The text of ``test/sql/examples.test``.

    Each published example must return at least one row. README statements must
    only bind and run: the mock scores by keyword overlap, so a threshold that
    selects rows against production may select none here.

    Returns:
        The sqllogictest file.
    """
    parts = [_HEADER]
    for label, sql in PUBLISHED:
        body = sql.strip().rstrip(";").strip()
        parts.append(f"# {' '.join(label.split())}\nquery I\nSELECT count(*) > 0 FROM (\n{body}\n);\n----\ntrue\n")
    parts.append(
        "# README walkthrough, against a small `tickets` table.\n"
        "statement ok\n"
        "CREATE TABLE tickets AS SELECT * FROM (VALUES "
        "(1, 'My package never arrived and tracking has not updated', 'gold'), "
        "(2, 'I was charged twice on my invoice', 'free')) t(id, body, tier);\n"
    )
    for number, block in enumerate(_readme_walkthrough_sql(), start=1):
        for statement in _statements(block):
            parts.append(f"# README block {number}\nstatement ok\n{statement};\n")
    return "\n".join(parts)


def test_the_sql_suite_runs_every_published_example() -> None:
    """test/sql/examples.test executes exactly what this worker publishes today."""
    expected = render_examples_test()
    if os.environ.get("UPDATE_SQL_TESTS") == "1":
        EXAMPLES_TEST.write_text(expected)
    assert EXAMPLES_TEST.read_text() == expected, (
        "test/sql/examples.test is stale; regenerate it: UPDATE_SQL_TESTS=1 uv run pytest tests/test_examples.py"
    )


class TestTheExampleSurfaceItself:
    """Properties of the set of examples, not of any one example."""

    def test_the_two_carriers_agree_by_sql_text(self) -> None:
        """vgi-lint merges Meta.examples and vgi.example_queries by normalised SQL.

        If they differ by so much as whitespace the client is shipped two copies
        of the example, one of them without a description.
        """
        for function in FUNCTIONS:
            native = {" ".join(e.sql.split()) for e in function.Meta.examples}
            tagged = {" ".join(e["sql"].split()) for e in json.loads(function.Meta.tags["vgi.example_queries"])}
            assert native == tagged, f"{function.Meta.name}: Meta.examples and vgi.example_queries diverge"

    def test_every_example_is_catalog_qualified(self) -> None:
        """An unqualified name resolves only if the user attached us as `typesafe`."""
        unqualified = [label for label, sql in PUBLISHED if "typesafe.main." not in sql]
        assert not unqualified, f"not catalog-qualified: {unqualified}"

    def test_every_function_is_demonstrated(self) -> None:
        """A function nobody shows an example of is a function nobody discovers."""
        blob = " ".join(sql for _, sql in PUBLISHED)
        for function in FUNCTIONS:
            assert f"typesafe.main.{function.Meta.name}(" in blob, f"{function.Meta.name} has no example"

    def test_every_agent_task_has_a_grader(self) -> None:
        """A published task with no private grader cannot be scored by `vgi-lint simulate`."""
        published = {t["name"] for t in json.loads(worker_module._CATALOG_TAGS["vgi.agent_test_tasks"])}
        graded = {t["name"] for t in _agent_graders()}
        assert published == graded, f"tasks without graders: {published - graded}"

    def test_published_tasks_expose_no_grader_fields(self) -> None:
        """VGI416: an agent under measurement must not be able to read the answer key."""
        for task in json.loads(worker_module._CATALOG_TAGS["vgi.agent_test_tasks"]):
            assert set(task) == {"name", "prompt"}, f"{task['name']} leaks {set(task) - {'name', 'prompt'}}"
