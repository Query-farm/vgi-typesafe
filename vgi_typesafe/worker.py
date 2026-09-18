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

    -- questions that differ per row: both arguments are input columns, so the
    -- answers can only come back as JSON
    SELECT t.id, a.answers->'dept'->>'choice'
    FROM queue t, LATERAL typesafe.main.ask_dynamic(t.body, t.questions) a;

    -- the one-question shorthands, one per question type
    SELECT t.id, c.choice, c.confidence
    FROM tickets t,
         LATERAL typesafe.main.choice(t.body,
             instructions => 'Which team should handle this?',
             criteria => MAP {'shipping': '...', 'billing': '...'}) c;

    SELECT n.noul FROM tickets t,
         LATERAL typesafe.main.noul(t.body, instructions => 'Needs a reply today?') n;

    SELECT s.score FROM tickets t,
         LATERAL typesafe.main.score(t.body, instructions => 'How bad is it?',
             criteria => ['minor', 'disruptive', 'critical']) s;

    -- a noul answer is one number, so it is also a plain expression
    SELECT * FROM tickets WHERE typesafe.main.is_true(body, 'Is this urgent?') > 0.8;

    -- what may go in `model =>`
    SELECT name, description, release_date FROM typesafe.main.models();

Function names are bare (``ask``, not ``typesafe_ask``) because they are already
qualified by the catalog they live in.
"""

from __future__ import annotations

import json
import sys

from vgi import Worker
from vgi.catalog import Catalog, ReadOnlyCatalogInterface, Schema, Table
from vgi.catalog.catalog_interface import CatalogInfo

from vgi_typesafe import __version__, auth
from vgi_typesafe.ask import AskFunction
from vgi_typesafe.ask_dynamic import AskDynamicFunction
from vgi_typesafe.choice import ChoiceFunction
from vgi_typesafe.is_true import IsTrueFunction
from vgi_typesafe.meta import column_comments, docs, examples, keywords
from vgi_typesafe.models import MODELS_SCHEMA, ModelsFunction
from vgi_typesafe.noul import NoulFunction
from vgi_typesafe.score import ScoreFunction

IMPLEMENTATION_VERSION = __version__
DATA_VERSION_SPEC = f"=={__version__}"
SOURCE_URL = "https://github.com/Query-farm/vgi-typesafe"

_KEYWORDS = keywords(
    "typesafe",
    "system one",
    "classification",
    "choice",
    "noul",
    "score",
    "yes/no",
    "scale",
    "routing",
    "confidence",
    "jev",
    "models",
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
        },
        {
            "name": "reference",
            "title": "Reference & Discovery",
            "description": (
                "What the account can ask for — the models a question may name, listed from SQL "
                "instead of from TypeSafe's documentation."
            ),
            "keywords": ["models", "discovery", "catalog", "reference"],
        },
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
                "Route this support message to the right team — shipping, billing or returns — and "
                "tell me how confident the model was: 'My package never arrived and the tracking "
                "has not updated in a week.'"
            ),
        },
        {
            "name": "several_judgments_one_request",
            "prompt": (
                "For the message 'I was charged twice and nobody has replied to me in three days', "
                "I want three things at once: which team should handle it, whether the customer "
                "sounds angry, and how severe it is on a three-point scale. Do it without paying "
                "for three separate API calls."
            ),
        },
        {
            "name": "ask_each_row_its_own_question",
            "prompt": (
                "My queue stores the question it wants asked alongside each row, and they are not "
                "all the same kind of question — one row wants its ticket routed to a team, the "
                "next wants a yes/no. I cannot hard-code one fixed set of questions for the whole "
                "query. How do I ask each row the question it carries, and read the answers back?"
            ),
        },
        {
            "name": "route_only_the_confident_ones",
            "prompt": (
                "Classify these two messages into shipping or billing — 'my package is lost in "
                "delivery' and 'my invoice shows a wrong charge' — but only keep the rows the model "
                "was confident about, so the rest can go to a human."
            ),
        },
        {
            "name": "judge_a_whole_row",
            "prompt": (
                "I have rows where several columns matter together — a message, a customer tier and "
                "an order id. Ask one yes/no question about the whole row rather than just the "
                "message column."
            ),
        },
        {
            "name": "find_out_what_columns_come_back",
            "prompt": (
                "Before I run this against real data: what columns and types come back if I ask a "
                "choice question and a yes/no question about the same row?"
            ),
        },
        {
            "name": "flag_which_messages_need_a_reply",
            "prompt": (
                "For these two messages — 'My package never arrived and nobody has replied in "
                "three days' and 'Thanks, just a routine question' — I do not want a yes or a no. "
                "I want to know how likely each one is to need a reply today, so I can pick my own "
                "cut-off later."
            ),
        },
        {
            "name": "rate_severity_on_a_scale",
            "prompt": (
                "Rate 'The whole site is down and no orders can be placed' on a three-point "
                "severity scale running from a minor question up to a critical outage, and tell me "
                "how sure the model was."
            ),
        },
        {
            "name": "filter_rows_by_a_yes_no_question",
            "prompt": (
                "From these two messages — 'My package never arrived' and 'Thanks for the quick "
                "refund' — keep only the ones about a lost package. I want the AI judgment inline "
                "in the WHERE clause, not joined in as a separate table."
            ),
        },
        {
            "name": "which_models_can_i_ask_for",
            "prompt": (
                "I do not want to be stuck on whatever model is the default. Which models can I "
                "actually name here, and when was each one published?"
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
        "of options — ticket routing, intent detection, content labelling — to answer a yes/no "
        "question as a probability rather than a verdict, or to place a row on an ordered scale. "
        "Every answer carries a calibrated confidence. `models()` lists the models a question may "
        "name. Requires a `typesafe` secret holding an API key."
    ),
    "vgi.doc_md": (
        "TypeSafe evaluates typed questions against a *state* — the content being judged — and "
        "answers with structured values rather than prose.\n\n"
        "### The two things you write\n\n"
        "A **state** is what you are judging: a piece of text, a struct built from several "
        "columns, a whole row, or a list. Descriptive field names help, because they tell the "
        "model how the parts relate.\n\n"
        "A **question** has a type, an instruction, and usually criteria. There are three types. "
        "A *choice* picks one option from a set you define and reports a probability for every "
        "option. A *noul* answers yes or no as a probability between 0 and 1, where 0.5 means "
        "genuinely undecided. A *score* places the state on an ordered scale you define, and "
        "returns a probability-weighted position rather than a single rung.\n\n"
        "### Reading an answer\n\n"
        "Every judgment except a noul carries a `confidence`: how concentrated the probability "
        "distribution was. It is the field to filter on when you want to route the clear cases "
        "automatically and send the rest to a person. A noul needs no such field, because a "
        "probability near 0.5 already says the model could not decide.\n\n"
        "### Authentication\n\n"
        "Credentials come from a redacted DuckDB secret; its optional `base_url` field points the "
        "worker at another endpoint, such as the bundled mock server "
        "(`uv run vgi-typesafe-mock`). See a question function's examples for the statement.\n\n"
        "### Cost\n\n"
        "One API request per distinct piece of work in an input batch — a state together with the "
        "questions asked of it — however many questions that is, so asking five things about a row "
        "costs the same as asking one. Requests within a batch run concurrently; rate-limit and "
        "overload responses are retried with backoff."
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
        (
            "Rate one text column on an ordered scale",
            "SELECT score, confidence FROM typesafe.main.score('The whole site is down', "
            "instructions => 'How severe is this?', "
            "criteria => ['minor question', 'disruptive delay', 'critical outage'])",
        ),
        (
            "Keep only the rows a yes/no question is true of",
            "SELECT t.body FROM (VALUES ('My package never arrived'), ('Thanks for the quick refund')) "
            "t(body) WHERE typesafe.main.is_true(t.body, 'Is this message about a lost package?') > 0.5",
        ),
        (
            "List the models a question may name",
            "SELECT name, description, release_date FROM typesafe.main.models() ORDER BY name",
        ),
    ),
    "vgi.doc_llm": (
        "Turn a row into a typed judgment with a calibrated confidence, instead of into prose. "
        "You supply the content being judged — a text column, a struct built from several "
        "columns, or the whole row — and one or more questions. A question either picks an option "
        "from a set you define, answers yes/no as a probability, or places the row on a scale you "
        "define. Ask several things about the same row together and they cost one request, not "
        "one each. Everything here takes the content as its first argument, so it composes under "
        "a correlated LATERAL to judge a whole table; filter on the reported confidence to route "
        "the clear cases and leave the rest to a person. Requires a `typesafe` secret."
    ),
    "vgi.doc_md": (
        "Typed judgments over rows, backed by TypeSafe's System One models.\n\n"
        "### Choosing a shape\n\n"
        "Asking several things about the same row is cheaper as one call than as several — the "
        "cost is one request per state, whatever it carries — so prefer the combined form when "
        "you want more than one judgment. Reach for a single-question form when you want one "
        "judgment and flat columns rather than a `STRUCT` to unpack. Reach for the scalar when "
        "the judgment is a yes/no probability you want to drop straight into a predicate: it is "
        "the same request either way, so this is about how the SQL reads, not what is possible.\n\n"
        "One form goes further and takes the questions themselves as a per-row column, so different "
        "rows can be asked different things. It pays for that by returning its answers as JSON "
        "rather than as typed columns, which makes it the one to reach for last — and only when the "
        "questions genuinely vary.\n\n"
        "### What they share\n\n"
        "All of them take the content as their first argument, which makes them blended table "
        "functions: one registration serves a literal call, an implicit lateral and an explicit "
        "one alike. Each produces exactly one output row per input row; a NULL input is never "
        "sent and answers NULL; identical inputs within a batch are asked once; and an API "
        "failure raises rather than quietly becoming NULL, which would be indistinguishable from "
        "a NULL input.\n\n"
        "### Before you spend anything\n\n"
        "A question written as a plan-time argument is validated when the query is planned, so a "
        "malformed one fails before a single row is billed. A question that arrives as data can "
        "only be checked as each row is read, and its errors say which row. `DESCRIBE` a call to "
        "see its result columns without issuing a request at all."
    ),
}

_MODEL_KEYWORDS = keywords("models", "model", "jev", "jev-latest", "jev-preview", "versions", "discovery")

#: Documentation for the `models` TABLE. It scans the same function and returns
#: the same rows, so the prose differs only where the two forms do: this one
#: reads without parentheses, which is what a parameterless listing should look
#: like in SQL. `vgi.result_columns_schema` is deliberately absent — a table
#: declares its columns to DuckDB directly, and that tag is function-scoped.
_MODELS_TABLE_DOCS = docs(
    category="reference",
    llm=(
        "Every TypeSafe model this key may name, as a plain table — the same rows `models()` "
        "returns, without the parentheses. Read it before setting `model =>` on any question "
        "function: they default to `jev-latest`, and this is the only place in SQL that says "
        "what else is accepted. Scanning it calls the API but bills no tokens."
    ),
    md=(
        "The model catalog, readable as a table.\n\n"
        "This table and the `models()` function are the same scan, returning the same rows. The "
        "table form exists because a listing that takes no arguments reads better without "
        "parentheses — see this table's example queries for the exact statement.\n\n"
        "### Using a name you find here\n\n"
        "`model =>` is a bind-time argument on every question function, so it takes a literal: "
        "paste the `name` into the call rather than joining this table into it.\n\n"
        "### Cost and freshness\n\n"
        "A `GET` that judges nothing and bills no tokens. Advertised as cacheable for five "
        "minutes, so repeated scans in one session cost one request."
    ),
    example_queries=examples(
        (
            "List every model without parentheses",
            "SELECT name, description, release_date FROM typesafe.main.models ORDER BY name",
        ),
        (
            "Which models are previews rather than the stable line",
            "SELECT name, release_date FROM typesafe.main.models WHERE name LIKE '%preview%' ORDER BY name",
        ),
    ),
    # A table is faceted and searched like a table, not like a function: the
    # same provider/domain pair the catalog and schema carry, plus its own
    # search terms.
    extra={"provider": "typesafe", "domain": "ai-classification", "vgi.keywords": _MODEL_KEYWORDS},
)

_TYPESAFE_CATALOG = Catalog(
    name="typesafe",
    default_schema="main",
    comment="TypeSafe System One questions (choice, noul, score) as LATERAL-joinable table functions",
    tags=_CATALOG_TAGS,
    source_url=SOURCE_URL,
    schemas=[
        Schema(
            path=["main"],
            comment="TypeSafe question functions and the model listing — require a 'typesafe' secret",
            tags=_SCHEMA_TAGS,
            functions=[
                AskFunction,
                AskDynamicFunction,
                ChoiceFunction,
                NoulFunction,
                ScoreFunction,
                IsTrueFunction,
                ModelsFunction,
            ],
            # `models` is registered twice on purpose, and the two forms serve
            # the same scan. As a *function* it matches the rest of this
            # catalog, and it is what the docs and examples call. As a *table*
            # it reads the way parameterless reference data should — `SELECT *
            # FROM typesafe.main.models`, no parentheses — which is what
            # vgi-lint's VGI311 asks for. DuckDB keeps tables and table
            # functions in separate catalog sets, so one name can be both;
            # tests/test_end_to_end.py pins that both resolve and agree.
            tables=[
                Table(
                    name="models",
                    function=ModelsFunction,
                    comment="The TypeSafe models a question may name (small, slow-changing reference data)",
                    tags=_MODELS_TABLE_DOCS,
                    column_comments=column_comments(MODELS_SCHEMA),
                    # A model id is the row's identity — it is what `model =>`
                    # takes, so two rows sharing one would make the listing
                    # useless. `_parse_model` refuses a nameless model, and a
                    # description missing upstream is read as an empty one, so
                    # neither column can arrive NULL.
                    primary_key=(("name",),),
                    not_null=("name", "description"),
                    # Two rows today, and every model TypeSafe has ever
                    # published is still listed. An estimate that is wrong by a
                    # few still beats the planner assuming a scan of unknown size.
                    cardinality_estimate=8,
                ),
            ],
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
