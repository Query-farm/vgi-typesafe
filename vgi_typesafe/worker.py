# Copyright 2026 Query Farm LLC - https://query.farm

"""VGI worker exposing TypeSafe System One questions to DuckDB/SQL.

    ATTACH 'typesafe' (TYPE vgi, LOCATION 'uv run typesafe_worker.py');
    CREATE SECRET (TYPE typesafe, api_key '...');

    -- several questions about each row, one request per row
    SELECT t.id, a.dept.choice, a.urgent.noul
    FROM tickets t,
         LATERAL typesafe.main.ask(t, questions => {
             'dept':   {'type': 'choice', 'instructions': '...', 'criteria': {...}},
             'urgent': {'type': 'noul',   'instructions': '...'}}) a;

    -- the one-question shorthand
    SELECT t.id, c.choice, c.confidence
    FROM tickets t,
         LATERAL typesafe.main.choice(t.body,
             instructions => 'Which team should handle this?',
             criteria => MAP {'shipping': '...', 'billing': '...'}) c;

Function names are bare (``ask``, not ``typesafe_ask``) because they are already
qualified by the catalog they live in.
"""

from __future__ import annotations

import json
import sys

from vgi import Worker
from vgi.catalog import Catalog, ReadOnlyCatalogInterface, Schema
from vgi.catalog.catalog_interface import CatalogInfo

from vgi_typesafe import __version__, auth
from vgi_typesafe.ask import AskFunction
from vgi_typesafe.choice import ChoiceFunction
from vgi_typesafe.meta import examples, keywords

IMPLEMENTATION_VERSION = __version__
DATA_VERSION_SPEC = f"=={__version__}"
SOURCE_URL = "https://github.com/Query-farm/vgi-typesafe"

_KEYWORDS = keywords(
    "typesafe", "system one", "classification", "choice", "noul", "score", "routing", "confidence", "jev"
)

_CATEGORIES = json.dumps(
    [
        {
            "name": "classification",
            "title": "Classification & Scoring",
            "description": (
                "Typed judgments about a row: pick one option, answer yes/no, or place it on an "
                "ordered scale — each with a calibrated confidence."
            ),
            "keywords": ["classify", "route", "score", "flag", "confidence"],
        }
    ]
)

#: PUBLIC task list only — `{name, prompt}`. The graders (reference_sql,
#: success_criteria) live in vgi-agent-tests.yaml so an agent being measured
#: cannot read the answer key out of the catalog it is querying (VGI416).
_AGENT_TEST_TASKS = json.dumps(
    [
        {
            "name": "route_a_support_ticket",
            "prompt": (
                "I have a table of support tickets with a body column. Route each one to the "
                "shipping, billing or returns team, and tell me how confident the model was."
            ),
        },
        {
            "name": "several_judgments_one_request",
            "prompt": (
                "For each support ticket I want three things at once: which team should handle it, "
                "whether the customer sounds angry, and how severe it is on a three-point scale. "
                "Do it without paying for three separate API calls per row."
            ),
        },
        {
            "name": "route_only_the_confident_ones",
            "prompt": (
                "Classify these tickets, but only auto-route the ones the model is confident about "
                "— send the rest to a human review queue."
            ),
        },
        {
            "name": "judge_a_whole_row",
            "prompt": (
                "My rows have several columns that matter together (message, customer tier, order "
                "id). Ask a question about the whole row rather than just one text column."
            ),
        },
        {
            "name": "find_out_what_columns_come_back",
            "prompt": (
                "Before I run this against real data, what columns and types will I get back if I "
                "ask a choice question and a yes/no question about the same row?"
            ),
        },
    ]
)

_EXECUTABLE_EXAMPLES = json.dumps(
    [
        {
            "name": "ask_shapes_its_columns_from_the_questions",
            "description": (
                "The result columns are built from the `questions` argument at bind time — one "
                "typed STRUCT per question, then `usage`. DESCRIBE only binds, so this runs "
                "without an API key and issues no request."
            ),
            "sql": (
                "DESCRIBE SELECT * FROM typesafe.main.ask('a lost package', questions => "
                "{'dept': {'type': 'choice', 'instructions': 'Which team?', "
                "'criteria': {'shipping': 'Delays and lost packages', 'billing': 'Invoices'}}, "
                "'urgent': {'type': 'noul', 'instructions': 'Does this need a reply today?'}})"
            ),
            "expected_result": [
                {
                    "column_name": "dept",
                    "column_type": "STRUCT(choice VARCHAR, confidence DOUBLE, probabilities MAP(VARCHAR, DOUBLE))",
                    "null": "YES",
                    "key": None,
                    "default": None,
                    "extra": None,
                },
                {
                    "column_name": "urgent",
                    "column_type": "STRUCT(noul DOUBLE)",
                    "null": "YES",
                    "key": None,
                    "default": None,
                    "extra": None,
                },
                {
                    "column_name": "usage",
                    "column_type": "STRUCT(model VARCHAR, input_tokens BIGINT, output_tokens BIGINT)",
                    "null": "YES",
                    "key": None,
                    "default": None,
                    "extra": None,
                },
            ],
        }
    ]
)

_CATALOG_TAGS = {
    "provider": "typesafe",
    "domain": "ai-classification",
    "vgi.title": "TypeSafe System One",
    "vgi.source_url": SOURCE_URL,
    "vgi.author": "Query Farm LLC <hello@query.farm>",
    "vgi.license": "MIT",
    "vgi.copyright": (
        "Worker (c) 2026 Query Farm LLC - https://query.farm. Judgments are produced by "
        "TypeSafe's System One models and are subject to TypeSafe's terms of use."
    ),
    "vgi.support_contact": "https://github.com/Query-farm/vgi-typesafe/issues",
    "vgi.support_policy_url": "https://github.com/Query-farm/vgi-typesafe/blob/main/README.md",
    "vgi.agent_test_tasks": _AGENT_TEST_TASKS,
    "vgi.executable_examples": _EXECUTABLE_EXAMPLES,
    "vgi.keywords": _KEYWORDS,
    "vgi.doc_llm": (
        "Structured AI judgments from TypeSafe's System One model, as SQL. Unlike a text-generating "
        "LLM, it answers a typed question about a piece of content and returns a value software "
        "can consume directly. Reach for this catalog to classify rows of a table into a fixed set "
        "of options — ticket routing, intent detection, content labelling — and get a calibrated "
        "confidence with each answer. Requires a `typesafe` secret holding an API key."
    ),
    "vgi.doc_md": (
        "TypeSafe evaluates typed questions against a *state* (the content to judge) and returns "
        "structured results rather than prose.\n\n"
        "### What is here\n\n"
        "- `ask()` — any number of questions about each row, answered in a single request per "
        "row, returning one typed `STRUCT` column per question. Three question types: `choice` "
        "(pick one option), `noul` (a yes/no probability) and `score` (a position on an ordered "
        "scale). The state can be a string, a struct, a list, or the whole row.\n"
        "- `choice()` — the one-question shorthand, with flat output columns.\n\n"
        "Both are blended table functions, so they compose under a correlated `LATERAL` to "
        "judge a whole table in one query.\n\n"
        "### Authentication\n\n"
        "The key is redacted in `duckdb_secrets()`. The optional `base_url` field points the "
        "worker at another endpoint, such as the bundled mock server "
        "(`uv run vgi-typesafe-mock`).\n\n"
        "```sql\n"
        "CREATE SECRET (TYPE typesafe, api_key '...');\n"
        "```\n\n"
        "### Cost\n\n"
        "One API request per distinct state per input batch. Requests within a batch run "
        "concurrently; rate-limit and overload responses are retried with backoff."
    ),
}

_SCHEMA_TAGS = {
    "provider": "typesafe",
    "domain": "ai-classification",
    "vgi.title": "TypeSafe Questions",
    "vgi.keywords": _KEYWORDS,
    "vgi.categories": _CATEGORIES,
    "vgi.example_queries": examples(
        (
            "Route, flag and grade every row in one request each",
            "SELECT t.id, a.dept.choice, a.urgent.noul FROM (VALUES (1, 'My package is lost')) "
            "t(id, body), LATERAL typesafe.main.ask(t.body, questions => "
            "{'dept': {'type': 'choice', 'instructions': 'Which team?', "
            "'criteria': {'shipping': 'Lost packages', 'billing': 'Invoices'}}, "
            "'urgent': {'type': 'noul', 'instructions': 'Needs a reply today?'}}) a",
        ),
        (
            "Classify one text column with the single-question shorthand",
            "SELECT choice, confidence FROM typesafe.main.choice('I was charged twice', "
            "instructions => 'Which team should handle this?', "
            "criteria => MAP {'shipping': 'Lost packages', 'billing': 'Invoices'})",
        ),
    ),
    "vgi.doc_llm": (
        "Both functions in this schema turn one row into a typed judgment. Reach for `ask()` when "
        "you want several judgments about the same row — it sends one request per row no matter "
        "how many questions you attach, and returns one `STRUCT` column per question. Reach for "
        "`choice()` when you want exactly one option picked from a set and prefer flat columns. "
        "Both take the content as their first argument, so they compose under a correlated LATERAL "
        "to judge a whole table. Every answer carries a confidence you can filter on."
    ),
    "vgi.doc_md": (
        "Two table functions over TypeSafe's System One models.\n\n"
        "### Which to use\n\n"
        "- `ask()` — any number of questions per row (`choice`, `noul`, `score`), one request per "
        "row, one typed `STRUCT` column per question plus a `usage` column.\n"
        "- `choice()` — the one-question shorthand, returning flat columns.\n\n"
        "### Shared behaviour\n\n"
        "Both are blended table functions: one registration serves a literal call, an implicit "
        "lateral and an explicit one alike — see this schema's example queries for each shape. "
        "Both produce exactly one output row per input row, "
        "skip the request entirely for a NULL input, ask once for repeated inputs within a batch, "
        "and raise on an API error rather than degrading to NULL. Both need a `typesafe` secret."
    ),
}

_TYPESAFE_CATALOG = Catalog(
    name="typesafe",
    default_schema="main",
    comment="TypeSafe System One questions (choice, noul, score) as LATERAL-joinable table functions",
    tags=_CATALOG_TAGS,
    source_url=SOURCE_URL,
    schemas=[
        Schema(
            path=["main"],
            comment="TypeSafe question functions — require a 'typesafe' secret",
            tags=_SCHEMA_TAGS,
            functions=[AskFunction, ChoiceFunction],
        ),
    ],
)


class TypeSafeCatalog(ReadOnlyCatalogInterface):
    """Advertises the worker's versions and the ``typesafe`` secret type."""

    catalog = _TYPESAFE_CATALOG
    catalog_name = _TYPESAFE_CATALOG.name
    secret_types = [auth.SECRET_SPEC]

    def catalogs(self) -> list[CatalogInfo]:
        """Advertise the single TypeSafe catalog."""
        return [
            CatalogInfo(
                name=self._effective_catalog_name,
                implementation_version=IMPLEMENTATION_VERSION,
                data_version_spec=DATA_VERSION_SPEC,
                source_url=SOURCE_URL,
            )
        ]


class TypeSafeWorker(Worker):
    """Worker process hosting the TypeSafe catalog."""

    catalog = _TYPESAFE_CATALOG
    catalog_interface = TypeSafeCatalog


def main() -> None:
    """Run the worker (stdio by default; pass ``--http`` for the HTTP server)."""
    TypeSafeWorker.main()


def main_http() -> None:
    """Run the worker over HTTP."""
    argv = sys.argv[1:]
    if "--http" not in argv:
        argv = ["--http", *argv]
    sys.argv = [sys.argv[0], *argv]
    TypeSafeWorker.main()


if __name__ == "__main__":
    main()
