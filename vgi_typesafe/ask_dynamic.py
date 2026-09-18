# Copyright 2026 Query Farm LLC - https://query.farm

"""``ask_dynamic()`` — questions chosen per row, answered as JSON.

``ask()`` takes its questions as a *named* argument, so they are fixed when the
query is planned and it can hand back one typed column per question.
``ask_dynamic()`` takes them positionally instead, which for a blended
(:class:`~vgi.table_in_out_function.RowTransformFunction`) function means they
are a per-row **input column**::

    SELECT t.id, a.answers->'dept'->>'choice'
    FROM tickets t, LATERAL typesafe.main.ask_dynamic(t.body, t.questions) a;

Two rows in one scan may therefore ask entirely different things, and that is the
whole point — but it also means nothing about the result shape is knowable at
bind. So the answers come back as one JSON object keyed by question name, which
the caller takes apart with DuckDB's JSON functions. Only the three values whose
shape never varies — the model and the two token counts — stay real columns.

Which form to write the questions in
------------------------------------
A JSON string is the form that genuinely handles heterogeneous questions: each
row carries its own shape verbatim. A ``STRUCT`` or ``MAP`` column works too, but
DuckDB unifies a column's type across rows, so a question only some rows ask is
padded onto all of them as NULL — survivable, because
:func:`vgi_typesafe.ask._plain` drops NULLs before the request — and two rows
that give one question name two different criteria shapes (a choice's map and a
score's list) cannot be unified at all, which is a binder error before this
worker sees anything. JSON text has neither constraint.

Validation moves from bind to process, because the questions are now data rather
than a plan-time constant. :func:`vgi_typesafe.ask.questions_of` does the work,
so the rules and the messages are the same ones ``ask()`` enforces; this module
adds which row they came from.

De-duplication moves too. ``ask()`` can key a batch on the state alone, since
every row asks the same thing. Here the same text may be asked two different
things, so :func:`vgi_typesafe.typesafe_api.ask_pairs` keys on the
``(state, questions)`` pair — one batching and retry path for both functions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import AnyArrow, Arg, SecretLookupEntry
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import BindParams, ProcessParams
from vgi.table_in_out_function import RowTransformFunction
from vgi_rpc.rpc import OutputCollector

from vgi_typesafe import ask, auth
from vgi_typesafe import typesafe_api as api
from vgi_typesafe.meta import docs, examples, field

#: The name every error message from this module is prefixed with.
ORIGIN = "ask_dynamic()"

#: How much of a malformed questions value an error message quotes back.
EXCERPT_LIMIT = 200

ANSWERS_SCHEMA = pa.schema(
    [
        field(
            "answers",
            pa.string(),
            "Every answer for this row as a JSON object keyed by question name; each value is the "
            "answer object for that question's type. NULL when the input state was NULL.",
        ),
        field("model", pa.string(), "The model that answered, as reported by the API."),
        field("input_tokens", pa.int64(), "Input tokens billed for this row's request."),
        field("output_tokens", pa.int64(), "Output tokens billed for this row's request."),
    ]
)

_CHOICE_QUESTION = (
    '{"dept": {"type": "choice", "instructions": "Which team should handle this?", '
    '"criteria": {"shipping": "Delivery status, delays, lost packages", '
    '"billing": "Charges, invoices, payment problems"}}}'
)
_NOUL_QUESTION = '{"urgent": {"type": "noul", "instructions": "Does this need a reply today?"}}'

_ROW_EXAMPLE = (
    "SELECT t.id, a.answers->'dept'->>'choice' AS dept, a.answers->'urgent'->>'noul' AS urgent "
    f"FROM (VALUES (1, 'My package is lost and delivery is delayed', '{_CHOICE_QUESTION}'), "
    f"(2, 'Thanks, just one routine question', '{_NOUL_QUESTION}')) t(id, body, questions), "
    "LATERAL typesafe.main.ask_dynamic(t.body, t.questions) a ORDER BY t.id"
)
_LITERAL_EXAMPLE = (
    "SELECT answers->'dept'->>'choice' AS dept, (answers->'dept'->>'confidence')::DOUBLE AS confidence "
    f"FROM typesafe.main.ask_dynamic('I was charged twice on my invoice', '{_CHOICE_QUESTION}')"
)


@dataclass(slots=True, frozen=True, kw_only=True)
class AskDynamicArgs:
    """``ask_dynamic(state, questions [, model =>, concurrency =>])``."""

    state: Annotated[AnyArrow, Arg(0, doc="The content to evaluate — a piece of text, a row, or a structured value")]
    questions: Annotated[
        AnyArrow,
        Arg(
            1,
            doc=(
                "This row's questions, keyed by the name each answer takes in the result. Every row "
                "may carry its own; see this function's documentation for the question types and "
                "the shape each expects."
            ),
        ),
    ]
    model: Annotated[str, Arg("model", doc="TypeSafe model id", default=api.DEFAULT_MODEL)] = api.DEFAULT_MODEL
    concurrency: Annotated[
        int,
        Arg("concurrency", doc="Max in-flight API requests per input batch", default=8, ge=1, le=64),
    ] = 8


def check_questions_type(kind: pa.DataType) -> None:
    """Reject a questions column that could not hold a question, at bind.

    The per-row values still have to be validated one by one, but a column of
    the wrong type is wrong for every row at once, and saying so at plan time
    beats saying it once the scan is under way.

    Args:
        kind: The Arrow type DuckDB resolved the second positional argument to.

    Raises:
        ValueError: The type can carry neither a question object nor JSON text.
    """
    text = pa.types.is_string(kind) or pa.types.is_large_string(kind)
    if not (text or pa.types.is_struct(kind) or pa.types.is_map(kind)):
        raise ValueError(
            f"{ORIGIN}: questions must be VARCHAR, STRUCT or MAP, not {kind}; write them as a "
            "struct keyed by question name, or as that same object in JSON text"
        )


def _excerpt(raw: Any) -> str:
    """As much of a rejected value as an error message can usefully quote back."""
    text = str(raw)
    return text if len(text) <= EXCERPT_LIMIT else f"{text[:EXCERPT_LIMIT]}..."


def questions_for_row(index: int, raw: Any, state: Any) -> dict[str, dict[str, Any]]:
    """Validate one row's questions, naming the row when they are unusable.

    ``ask()`` validates once, at bind, and a bad question there is a bad query.
    Here the questions are data: one row in a million can be the broken one, so
    every message has to say which row and what it held, or the user is left
    grepping their own input for it. Under a correlated LATERAL, DuckDB hands
    over one row at a time and the index is always 0 — which is why the message
    quotes the row's own content too, since that is what identifies it in the
    caller's table.

    Args:
        index: The row's position in the input batch being processed.
        raw: That row's questions value, exactly as DuckDB delivered it.
        state: That row's content, quoted back so the row can be found again.

    Returns:
        The row's questions in the shape :func:`vgi_typesafe.typesafe_api.ask`
        sends.

    Raises:
        ValueError: The value is missing, or breaks one of ``ask()``'s rules.
    """
    if raw is None:
        raise ValueError(
            f"{ORIGIN}: row {index} of this input batch carries no questions, so there is nothing "
            f"to ask about {_excerpt(state)}. Questions are a per-row argument here: every row with "
            "something to judge must carry its own — a struct keyed by question name, or that same "
            "object in JSON text."
        )
    try:
        # `reserved=None`: ask() forbids a question named after its trailing
        # `usage` column, but every answer here lives inside the JSON, so no
        # question name collides with an output column.
        return ask.questions_of(raw, origin=ORIGIN, reserved=None)
    except ValueError as exc:
        raise ValueError(f"{exc} — row {index} of this input batch, whose questions were {_excerpt(raw)}") from exc


class AskDynamicFunction(RowTransformFunction[AskDynamicArgs]):
    """Ask each row its own questions — strictly 1->1, one request per row."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ANSWERS_SCHEMA

    class Meta:
        """Catalog metadata: name, docs, and the examples clients copy."""

        name = "ask_dynamic"
        description = "Ask TypeSafe questions that can differ from row to row, answering each row as one JSON object"
        categories = ["classification", "scoring", "blended"]
        required_secrets = [SecretLookupEntry(secret_type=auth.SECRET_TYPE)]
        tags = docs(
            category="classification",
            result_schema=ANSWERS_SCHEMA,
            llm=(
                "Ask TypeSafe questions that are chosen per row rather than fixed for the whole "
                "query, and read each row's answers back as JSON. Both positional arguments are "
                "per-row input columns, so a column of questions may carry a different set — "
                "different names, different types, different criteria — on every row. That "
                "flexibility costs the typed columns `ask()` gives you: because no shape is known "
                "when the query is planned, all of a row's answers arrive inside one JSON value "
                "that DuckDB's JSON functions take apart. Prefer `ask()` whenever the questions are "
                "the same for every row; reach for this only when they genuinely vary, such as a "
                "rules table joined to the rows it governs. The question types and their answers "
                "are `ask()`'s: a choice reports the chosen option with a confidence and a "
                "distribution, a noul reports a probability, a score reports a position on the "
                "scale. Cost is one request per distinct pairing of content and questions. Needs a "
                "`typesafe` secret."
            ),
            md=(
                "Per-row questions, answered as JSON.\n\n"
                "### When to reach for this instead of `ask()`\n\n"
                "`ask()` is almost always the one you want. Its questions are fixed when the query "
                "is planned, so it can give you one typed column per question — a `STRUCT` whose "
                "fields suit that question's type, readable with a dot. `ask_dynamic()` trades "
                "those typed columns away. Here the questions are a per-row input column, so two "
                "rows in one scan may ask entirely different things, and nothing about the result "
                "shape can be known at plan time. Everything a row was told therefore comes back "
                "inside one JSON column that you take apart yourself.\n\n"
                "That makes this the more awkward of the two, and it should not be anyone's "
                "default. Use it when the questions genuinely vary per row: a rules table joined to "
                "the rows it governs, a work queue where each item carries its own rubric, "
                "questions assembled by an application. If every row asks the same thing, use "
                "`ask()` and keep the typed columns.\n\n"
                "### The two positional arguments\n\n"
                "Both are per-row input columns. The first is the content being judged, in the "
                "same shapes `ask()` accepts: text, a `STRUCT` (the row alias itself works), a "
                "`LIST` or a `MAP`, sent to the API as the equivalent JSON object or array.\n\n"
                "The second carries that row's questions, in either of two forms.\n\n"
                "- JSON text is the form to prefer when the questions really differ: each row "
                "carries its own shape verbatim, and nothing is reconciled against any other row.\n"
                "- A `STRUCT` or `MAP` column works too, and reads better when the questions vary "
                "only in their wording. DuckDB unifies a column's type across rows, though, so a "
                "question only some rows ask is padded onto all of them as NULL. Those NULLs are "
                "dropped before the request, so that much is harmless — but two rows that give one "
                "question name two different criteria shapes cannot be unified at all, and DuckDB "
                "rejects the query before this worker sees it.\n\n"
                "`model =>` and `concurrency =>` stay named, because a blended function's "
                "positional arguments are input columns rather than plan-time constants.\n\n"
                "### Reading the answers\n\n"
                "The `answers` column is a JSON object keyed by question name, and each value is "
                "the answer object for that question's type:\n\n"
                "| Question type | Answer object |\n| --- | --- |\n"
                "| choice | `choice`, `confidence`, and `probabilities` keyed by option |\n"
                "| noul | `noul`, a probability where near 1 is yes and 0.5 is genuinely "
                "undecided |\n"
                "| score | `score`, `confidence`, and `probabilities` keyed by level number, where "
                "0 is the first criterion |\n\n"
                "Keys in JSON are text, so a score level reads back as the digits of its number. "
                "DuckDB's JSON operators and the `json_extract` family are what take all this "
                "apart; this function's example queries show the exact spelling.\n\n"
                "What a row cost does not vary in shape, so it stays in real columns beside the "
                "JSON rather than inside it: `model`, `input_tokens` and `output_tokens`.\n\n"
                "### Row semantics\n\n"
                "Strictly one output row per input row. A NULL state — or a struct or list holding "
                "no non-null content — answers NULL and makes no request; its questions are not "
                "examined, because nothing is being asked of it. Any other row that carries no "
                "questions is an error that names the row and quotes what it held, since a per-row "
                "argument cannot be checked when the query is planned.\n\n"
                "Repeated work is collapsed on the pair, not on the content alone: two rows asking "
                "the same thing of the same content share one request, while the same content asked "
                "two different things is two requests. Errors raise — only a NULL input answers "
                "NULL, so a failure can never be mistaken for one.\n\n"
                "### Authentication\n\n"
                "Credentials come from a `typesafe` secret, or from `TYPESAFE_API_KEY` in the "
                "worker's environment."
            ),
            example_queries=examples(
                ("Ask each row the questions that row carries", _ROW_EXAMPLE),
                ("Pull one answer out of the JSON", _LITERAL_EXAMPLE),
            ),
        )
        examples = [
            FunctionExample(sql=_ROW_EXAMPLE, description="Ask each row the questions that row carries"),
            FunctionExample(sql=_LITERAL_EXAMPLE, description="Pull one answer out of the JSON"),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[AskDynamicArgs]) -> BindResponse:
        """Reject input columns that cannot carry a state or a question; the shape is fixed."""
        input_schema = params.bind_call.input_schema
        if input_schema is not None:
            if len(input_schema) > 0:
                ask.check_state_type(input_schema.field(0).type, parse_json=False, origin=ORIGIN)
            if len(input_schema) > 1:
                check_questions_type(input_schema.field(1).type)
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def process(
        cls,
        params: ProcessParams[AskDynamicArgs],
        state: None,
        batch: pa.RecordBatch,
        out: OutputCollector,
    ) -> None:
        """Answer each row's own questions, for one input batch."""
        states = ask.states_of(batch.column("state"), parse_json=False)
        raw_questions = batch.column("questions").to_pylist()
        pairs: list[api.Pair | None] = [
            # A row with nothing to judge is asked nothing, so its questions are
            # never looked at — an all-NULL batch must not need a key, nor
            # well-formed questions it will not send.
            None if row_state is None else (row_state, questions_for_row(index, raw, row_state))
            for index, (row_state, raw) in enumerate(zip(states, raw_questions, strict=True))
        ]

        responses: list[api.Response | None]
        if any(pair is not None for pair in pairs):
            # Resolved only when there is something to ask, so an all-NULL
            # batch succeeds without a key.
            credentials = auth.for_call(params.secrets)
            with api.open_client() as client:
                responses = api.ask_pairs(
                    pairs,
                    credentials=credentials,
                    client=client,
                    model=params.args.model or api.DEFAULT_MODEL,
                    concurrency=params.args.concurrency,
                )
        else:
            responses = [None] * len(pairs)

        columns: dict[str, list[Any]] = {
            "answers": [None if r is None else json.dumps(r.answers, ensure_ascii=False) for r in responses],
            "model": [r.model if r else None for r in responses],
            "input_tokens": [r.input_tokens if r else None for r in responses],
            "output_tokens": [r.output_tokens if r else None for r in responses],
        }
        full = pa.RecordBatch.from_pydict(columns, schema=cls.FIXED_SCHEMA)
        out.emit(full.select(params.output_schema.names))
