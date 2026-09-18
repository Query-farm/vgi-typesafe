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

Runs against the mock endpoint, so it costs nothing and needs no key.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator

import pytest

from tests.test_end_to_end import HAYBARN, PROJECT
from vgi_typesafe import worker as worker_module
from vgi_typesafe.ask import AskFunction
from vgi_typesafe.choice import ChoiceFunction
from vgi_typesafe.is_true import IsTrueFunction
from vgi_typesafe.mock_server import MockTypeSafeServer, running
from vgi_typesafe.models import ModelsFunction
from vgi_typesafe.noul import NoulFunction
from vgi_typesafe.score import ScoreFunction

pytestmark = pytest.mark.e2e

FUNCTIONS = (AskFunction, ChoiceFunction, NoulFunction, ScoreFunction, IsTrueFunction, ModelsFunction)
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


@pytest.fixture(scope="module")
def mock() -> Iterator[MockTypeSafeServer]:
    """One mock endpoint for the whole module; these run read-only against it."""
    if not HAYBARN.is_file():
        pytest.skip(f"no haybarn binary at {HAYBARN}")
    with running(api_key="test-key") as server:
        yield server


def _run(mock: MockTypeSafeServer, sql: str) -> subprocess.CompletedProcess[str]:
    script = (
        ".output /dev/null\n"
        "ATTACH 'typesafe' (TYPE vgi, LOCATION 'uv run --no-sources typesafe_worker.py');\n"
        f"CREATE SECRET ts (TYPE typesafe, api_key 'test-key', base_url '{mock.base_url}');\n"
        ".output\n.mode json\n" + sql.rstrip().rstrip(";") + ";\n"
    )
    return subprocess.run(
        [str(HAYBARN), "-unsigned"], input=script, capture_output=True, text=True, timeout=180, cwd=PROJECT
    )


@pytest.mark.parametrize(("label", "sql"), PUBLISHED, ids=[label for label, _ in PUBLISHED])
def test_every_published_example_runs(mock: MockTypeSafeServer, label: str, sql: str) -> None:
    """A published example that does not run is a lie told to whoever copies it."""
    result = _run(mock, sql)
    assert result.returncode == 0 and not result.stderr.strip(), (
        f"{label} failed:\n{result.stderr.strip() or result.stdout.strip()}"
    )
    assert json.loads(result.stdout.strip() or "[]"), f"{label} returned no rows"


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
