# Copyright 2026 Query Farm LLC - https://query.farm

"""``noul()`` — a TypeSafe noul question as a LATERAL-joinable table function.

A *noul* is TypeSafe's yes/no primitive: the answer is one probability between 0
and 1, and there is nothing else to report — no chosen option, no distribution,
so no confidence either. ``noul()`` is the one-question shorthand for it, the
sibling of :mod:`vgi_typesafe.choice`::

    SELECT * FROM typesafe.main.noul(
        'My package never arrived and nobody has replied',
        instructions => 'Does this need a reply today?');

    SELECT t.id, n.noul
    FROM tickets t,
         LATERAL typesafe.main.noul(t.body,
             instructions => 'Does this need a reply today?') n;

Like ``choice()`` it is a **blended**
(:class:`~vgi.table_in_out_function.RowTransformFunction`) table-in-out function:
its positional argument *is* the per-row input column, so one registration serves
a literal call and a correlated LATERAL alike, the question itself arrives as
*named* bind-time arguments, and there is no ``finalize`` (DuckDB forbids one
under correlated LATERAL).

``criteria`` is optional here, and unlike a choice's it is not a list of things
to pick between: it describes what a yes looks like and what a no looks like, so
it carries only the keys ``true`` and ``false``. Either may be given alone. The
validation matches :func:`vgi_typesafe.ask.questions_of`, because the same
question reaches the same endpoint either way.

The map is strictly 1->1: every input row yields exactly one output row, and a
NULL state yields a row of NULLs without a request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg, SecretLookupEntry
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import BindParams, ProcessParams
from vgi.table_in_out_function import RowTransformFunction
from vgi_rpc.rpc import OutputCollector

from vgi_typesafe import auth
from vgi_typesafe import typesafe_api as api
from vgi_typesafe.meta import docs, examples, field

#: The name this function gives its single question in each request.
QUESTION_ID = "noul"

#: The only keys a noul's criteria may carry — one description per outcome.
OUTCOMES = ("true", "false")

NOUL_SCHEMA = pa.schema(
    [
        field(
            "noul",
            pa.float64(),
            "Probability between 0 and 1 that the answer is yes; near 1 is yes, near 0 is no, "
            "0.5 is genuinely undecided. NULL when the input state was NULL.",
        ),
        field("model", pa.string(), "The model that answered, as reported by the API."),
        field("input_tokens", pa.int64(), "Input tokens billed for this row's request."),
        field("output_tokens", pa.int64(), "Output tokens billed for this row's request."),
    ]
)

_LATERAL_EXAMPLE = (
    "SELECT t.body, n.noul "
    "FROM (VALUES ('My package never arrived'), ('Thanks for the quick refund')) t(body), "
    "LATERAL typesafe.main.noul(t.body, "
    "instructions => 'Is this message about a lost package?') n"
)
_CRITERIA_EXAMPLE = (
    "SELECT noul FROM typesafe.main.noul('My package never arrived and nobody has replied', "
    "instructions => 'Does this need a reply today?', "
    "criteria => MAP {'true': 'angry, waiting for days, money at stake', "
    "'false': 'a routine question that can wait'})"
)


@dataclass(slots=True, frozen=True, kw_only=True)
class NoulArgs:
    """``noul(state, instructions => [, criteria =>])``."""

    state: Annotated[str, Arg(0, doc="The content to evaluate — the per-row input column")]
    # Empty string means "not supplied": a `str | None` annotation resolves to
    # the Arrow null type, which DuckDB cannot cast a VARCHAR into. on_bind
    # names the omission rather than leaving it to the API.
    instructions: Annotated[
        str, Arg("instructions", doc="The yes/no question to answer about each state (required)", default="")
    ] = ""
    # Genuinely optional — a noul is answerable from `instructions` alone. The
    # explicit arrow_type keeps the SQL signature a MAP; without it `| None`
    # would resolve to the Arrow null type.
    criteria: Annotated[
        dict[str, str] | None,
        Arg(
            "criteria",
            arrow_type=pa.map_(pa.string(), pa.string()),
            doc="What a yes looks like and what a no looks like, keyed by outcome (optional)",
            default=None,
        ),
    ] = None
    model: Annotated[str, Arg("model", doc="TypeSafe model id", default=api.DEFAULT_MODEL)] = api.DEFAULT_MODEL
    concurrency: Annotated[
        int,
        Arg("concurrency", doc="Max in-flight API requests per input batch", default=8, ge=1, le=64),
    ] = 8


def criteria_of(raw: Any) -> dict[str, str] | None:
    """Normalise the optional ``criteria`` MAP argument into ``{outcome: description}``.

    An Arrow map scalar converts to a list of ``(key, value)`` pairs; a dict is
    accepted too so the function can be driven directly from Python.

    Args:
        raw: The ``criteria`` argument as DuckDB delivered it, or None.

    Returns:
        A description per outcome, or None when the caller supplied no criteria
        — which is a complete noul question, not an incomplete one.

    Raises:
        ValueError: The map carries a key that is not an outcome name.
    """
    if raw is None:
        return None
    pairs: list[tuple[Any, Any]]
    if isinstance(raw, dict):
        pairs = list(raw.items())
    else:
        pairs = [(p["key"], p["value"]) if isinstance(p, dict) else tuple(p) for p in raw]
    criteria: dict[str, str] = {}
    for outcome, description in pairs:
        key = "" if outcome is None else str(outcome).strip()
        if key not in OUTCOMES:
            raise ValueError(
                f"noul() criteria has the key {key!r}; a noul describes only its two outcomes, so the "
                "only keys are 'true' and 'false' — e.g. criteria => MAP {'true': 'needs a reply today', "
                "'false': 'can wait until next week'}. Either may be given on its own."
            )
        criteria[key] = "" if description is None else str(description)
    return criteria or None


def question_of(instructions: str, criteria: Any) -> dict[str, Any]:
    """Build the one question this function asks, validating both halves of it.

    Args:
        instructions: The ``instructions`` argument.
        criteria: The ``criteria`` argument as DuckDB delivered it, or None.

    Returns:
        The question in the shape :func:`vgi_typesafe.typesafe_api.ask_many` sends.

    Raises:
        ValueError: ``instructions`` is missing, or ``criteria`` is malformed.
    """
    if not instructions.strip():
        raise ValueError("noul() requires 'instructions', e.g. instructions => 'Does this need a reply today?'")
    question: dict[str, Any] = {"type": "noul", "instructions": instructions}
    outcomes = criteria_of(criteria)
    if outcomes:
        question["criteria"] = outcomes
    return question


class NoulFunction(RowTransformFunction[NoulArgs]):
    """Answer one yes/no question per input row — strictly 1->1."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = NOUL_SCHEMA

    class Meta:
        """Catalog metadata: name, docs, and the examples clients copy."""

        name = "noul"
        description = "Answer a yes/no question about each row as a calibrated probability, not a verdict"
        categories = ["classification", "blended"]
        required_secrets = [SecretLookupEntry(secret_type=auth.SECRET_TYPE)]
        tags = docs(
            category="classification",
            result_schema=NOUL_SCHEMA,
            llm=(
                "Ask one yes/no question about each row using TypeSafe's System One model, and get "
                "the probability that the answer is yes rather than a bare boolean. Pass the text as "
                "the positional argument — a literal, or a column under LATERAL to judge a whole "
                "table — and the question as the named `instructions` argument. Returns a `DOUBLE` "
                "between 0 and 1 per row: near 1 is yes, near 0 is no, 0.5 means the model genuinely "
                "cannot tell. Compare it against a threshold you choose, which is the point of "
                "getting a probability instead of a verdict. Optional `criteria` describes what a "
                "yes and a no look like. A noul answer has no separate confidence, because the "
                "number is already the whole answer. Use `is_true()` when you want the same number "
                "as a plain expression, and `ask()` when the row needs more than one question. "
                "Needs a `typesafe` secret."
            ),
            md=(
                "One TypeSafe yes/no question, asked of every input row.\n\n"
                "### Arguments\n\n"
                "- `state` (positional): the content to evaluate. A literal, or a column.\n"
                "- `instructions =>`: the question, phrased so that yes and no both make sense.\n"
                "- `criteria =>`: optional. A `MAP` with a `'true'` entry describing what a yes "
                "looks like and a `'false'` entry describing what a no looks like. Either may be "
                "given on its own, and no other key is accepted — a noul has exactly two outcomes, "
                "so this is a rubric rather than a set of things to choose between.\n"
                "- `model =>`: defaults to `jev-latest`.\n"
                "- `concurrency =>`: in-flight requests per batch (default 8).\n\n"
                "### Reading the answer\n\n"
                "`noul` is a probability, not a verdict: near 1 is yes, near 0 is no, and 0.5 is the "
                "model saying the content does not decide the question. That is why there is no "
                "`confidence` column here as there is on `choice()` and `score()` — a one-number "
                "answer carries its own certainty, and 0.5 is exactly what low confidence looks "
                "like. Pick the threshold that suits the cost of being wrong, and keep the rows "
                "near 0.5 for a human.\n\n"
                "### Row semantics\n\n"
                "Strictly one output row per input row. A NULL state yields a row of NULLs and "
                "makes no request. Identical states within a batch are asked once.\n\n"
                "### Errors\n\n"
                "An API failure raises — it never degrades to NULL, which would be "
                "indistinguishable from a NULL input. 429 and 529 are retried with backoff first.\n\n"
                "### Authentication\n\n"
                "Add `base_url` to the secret to target the bundled mock endpoint. "
                "`TYPESAFE_API_KEY` in the worker's environment is the fallback.\n\n"
                "```sql\n"
                "CREATE SECRET (TYPE typesafe, api_key '...');\n"
                "```"
            ),
            example_queries=examples(
                ("Flag every row of a table via LATERAL", _LATERAL_EXAMPLE),
                ("Describe both outcomes with criteria", _CRITERIA_EXAMPLE),
            ),
        )
        examples = [
            FunctionExample(sql=_LATERAL_EXAMPLE, description="Flag every row of a table via LATERAL"),
            FunctionExample(sql=_CRITERIA_EXAMPLE, description="Describe both outcomes with criteria"),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[NoulArgs]) -> BindResponse:
        """Reject an incomplete or malformed question at plan time, before any row is billed."""
        question_of(params.args.instructions, params.args.criteria)
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def process(
        cls,
        params: ProcessParams[NoulArgs],
        state: None,
        batch: pa.RecordBatch,
        out: OutputCollector,
    ) -> None:
        """Answer the question for each row of one input batch."""
        states = [None if s is None else str(s) for s in batch.column("state").to_pylist()]
        responses: list[api.Response | None]
        if any(s is not None for s in states):
            # Resolved only when there is something to ask, so an all-NULL
            # batch succeeds without a key.
            credentials = auth.for_call(params.secrets)
            with api.open_client() as client:
                responses = api.ask_many(
                    states,
                    {QUESTION_ID: question_of(params.args.instructions, params.args.criteria)},
                    credentials=credentials,
                    client=client,
                    model=params.args.model or api.DEFAULT_MODEL,
                    concurrency=params.args.concurrency,
                )
        else:
            responses = [None] * len(states)

        columns: dict[str, list[Any]] = {
            "noul": [r.answers[QUESTION_ID]["noul"] if r else None for r in responses],
            "model": [r.model if r else None for r in responses],
            "input_tokens": [r.input_tokens if r else None for r in responses],
            "output_tokens": [r.output_tokens if r else None for r in responses],
        }
        full = pa.RecordBatch.from_pydict(columns, schema=cls.FIXED_SCHEMA)
        out.emit(full.select(params.output_schema.names))
