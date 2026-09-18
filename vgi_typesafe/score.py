# Copyright 2026 Query Farm LLC - https://query.farm

"""``score()`` — a TypeSafe score question as a LATERAL-joinable table function.

A *score* is TypeSafe's ordered-scale primitive: you describe the rungs of a
ladder, lowest first, and the answer is a position on it — not a rung, a
position, weighted by how probable each rung was. ``score()`` is the
one-question shorthand for it, the sibling of :mod:`vgi_typesafe.choice`::

    SELECT * FROM typesafe.main.score('The whole site is down',
        instructions => 'How severe is this?',
        criteria => ['minor question', 'disruptive delay', 'critical outage']);

    SELECT t.id, s.score, s.confidence
    FROM tickets t,
         LATERAL typesafe.main.score(t.body,
             instructions => 'How severe is this?',
             criteria => ['minor question', 'disruptive delay', 'critical outage']) s;

Like ``choice()`` it is a **blended**
(:class:`~vgi.table_in_out_function.RowTransformFunction`) table-in-out function:
its positional argument *is* the per-row input column, so one registration serves
a literal call and a correlated LATERAL alike, the question itself arrives as
*named* bind-time arguments, and there is no ``finalize`` (DuckDB forbids one
under correlated LATERAL).

Where a choice's criteria is a MAP — options are named, and unordered — a
score's is an ordered DuckDB ``VARCHAR[]``: the position of a level in the list
*is* its meaning, and the answer is expressed in those positions. Level 0 is the
first element. That is also why ``probabilities`` is keyed by level number
rather than by the level's text: the text is prose that may repeat or be
rewritten, while the number is the scale.

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
QUESTION_ID = "score"

SCORE_SCHEMA = pa.schema(
    [
        field(
            "score",
            pa.float64(),
            "Probability-weighted position on the scale: 0 is the first criterion, and the last "
            "level's number is the top. NULL when the input state was NULL.",
        ),
        field("confidence", pa.float64(), "0-1 score of how concentrated the probability distribution is."),
        field(
            "probabilities",
            pa.map_(pa.int32(), pa.float64()),
            "Probability of every level, keyed by level number (0 is the first criterion); sums to 1.",
        ),
        field("model", pa.string(), "The model that answered, as reported by the API."),
        field("input_tokens", pa.int64(), "Input tokens billed for this row's request."),
        field("output_tokens", pa.int64(), "Output tokens billed for this row's request."),
    ]
)

_SCALE = "criteria => ['minor question', 'disruptive delay', 'critical outage']"
_LATERAL_EXAMPLE = (
    "SELECT t.body, s.score, s.confidence "
    "FROM (VALUES ('A quick question about my invoice'), ('The whole site is down')) t(body), "
    f"LATERAL typesafe.main.score(t.body, instructions => 'How severe is this?', {_SCALE}) s"
)
_LITERAL_EXAMPLE = (
    "SELECT score, probabilities FROM typesafe.main.score('The whole site is down', "
    f"instructions => 'How severe is this?', {_SCALE})"
)


@dataclass(slots=True, frozen=True, kw_only=True)
class ScoreArgs:
    """``score(state, instructions =>, criteria =>)``."""

    state: Annotated[str, Arg(0, doc="The content to evaluate — the per-row input column")]
    # Empty string means "not supplied": a `str | None` annotation resolves to
    # the Arrow null type, which DuckDB cannot cast a VARCHAR into. Both are
    # required, and on_bind says so by name rather than leaving it to the API.
    instructions: Annotated[
        str, Arg("instructions", doc="The question to answer about each state (required)", default="")
    ] = ""
    # Optional only so that omitting it reaches on_bind's explanation instead of
    # the framework's generic NULL rejection. The explicit arrow_type keeps the
    # SQL signature an ordered VARCHAR[]; without it `| None` would resolve to
    # the Arrow null type.
    criteria: Annotated[
        list[str] | None,
        Arg(
            "criteria",
            arrow_type=pa.list_(pa.string()),
            doc="The rungs of the scale in ascending order, lowest first, each described (required)",
            default=None,
        ),
    ] = None
    model: Annotated[str, Arg("model", doc="TypeSafe model id", default=api.DEFAULT_MODEL)] = api.DEFAULT_MODEL
    concurrency: Annotated[
        int,
        Arg("concurrency", doc="Max in-flight API requests per input batch", default=8, ge=1, le=64),
    ] = 8


def levels_of(raw: Any) -> list[str]:
    """Normalise the ``criteria`` list argument into the ordered scale.

    Args:
        raw: The ``criteria`` argument as DuckDB delivered it.

    Returns:
        One description per level, lowest first — the order the answer's
        ``probabilities`` are keyed by.

    Raises:
        ValueError: The scale is missing, too short to be a scale, longer than
            the API accepts, or has a blank level.
    """
    levels = list(raw) if isinstance(raw, (list, tuple)) else []
    low, high = api.MIN_SCORE_LEVELS, api.MAX_SCORE_LEVELS
    if not low <= len(levels) <= high:
        raise ValueError(
            f"score() requires 'criteria': an ordered list of {low}-{high} level descriptions, lowest first, "
            "e.g. criteria => ['minor question', 'disruptive delay', 'critical outage']; "
            f"got {len(levels)}. A scale needs at least two rungs to place anything between."
        )
    scale: list[str] = []
    for index, level in enumerate(levels):
        if level is None or not str(level).strip():
            raise ValueError(
                f"score() criteria level {index} is NULL or blank; every rung needs a description, "
                "because the description is all the model has to tell one rung from the next"
            )
        scale.append(str(level))
    return scale


def question_of(instructions: str, criteria: Any) -> dict[str, Any]:
    """Build the one question this function asks, validating both halves of it.

    Args:
        instructions: The ``instructions`` argument.
        criteria: The ``criteria`` argument as DuckDB delivered it.

    Returns:
        The question in the shape :func:`vgi_typesafe.typesafe_api.ask_many` sends.

    Raises:
        ValueError: ``instructions`` is missing, or the scale is malformed.
    """
    if not instructions.strip():
        raise ValueError("score() requires 'instructions', e.g. instructions => 'How severe is this?'")
    return {"type": "score", "instructions": instructions, "criteria": levels_of(criteria)}


class ScoreFunction(RowTransformFunction[ScoreArgs]):
    """Place each input row on an ordered scale — strictly 1->1."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = SCORE_SCHEMA

    class Meta:
        """Catalog metadata: name, docs, and the examples clients copy."""

        name = "score"
        description = "Place each row on an ordered scale you describe, as a probability-weighted position"
        categories = ["scoring", "blended"]
        required_secrets = [SecretLookupEntry(secret_type=auth.SECRET_TYPE)]
        tags = docs(
            category="classification",
            result_schema=SCORE_SCHEMA,
            llm=(
                "Rate each row on an ordered scale using TypeSafe's System One model. Pass the text "
                "as the positional argument — a literal, or a column under LATERAL to rate a whole "
                "table — and describe the scale once with the named `instructions` and `criteria` "
                "arguments, where `criteria` is an ordered list of level descriptions with the "
                "lowest first. Returns one row per input row: `score`, the probability-weighted "
                "position on that scale where 0 is the first level; a 0-1 `confidence`; and the "
                "full distribution as a `MAP` keyed by level number. Because the score is weighted "
                "rather than rounded, a row the model reads as between two levels lands between "
                "them, which is what makes the column sortable. Reach for `choice()` instead when "
                "the options have no order, and `ask()` when one row needs several questions. "
                "Needs a `typesafe` secret."
            ),
            md=(
                "One TypeSafe score question, asked of every input row.\n\n"
                "### Arguments\n\n"
                "- `state` (positional): the content to evaluate. A literal, or a column.\n"
                "- `instructions =>`: the question, e.g. `'How severe is this?'`.\n"
                "- `criteria =>`: the scale, as an ordered list of level descriptions with the "
                "lowest first. A scale needs at least two rungs, and the API caps it at ten. A "
                "level may be a plain description or a fuller rubric; what matters is that "
                "neighbouring rungs are told apart by something the content can actually show.\n"
                "- `model =>`: defaults to `jev-latest`.\n"
                "- `concurrency =>`: in-flight requests per batch (default 8).\n\n"
                "### Reading the answer\n\n"
                "`score` is a position, not a level: the probability-weighted average of the level "
                "numbers, so a row the model reads as halfway between the first and second rung "
                "scores 0.5. Round it to snap to a rung, or leave it alone to rank rows against "
                "each other. `probabilities` is keyed by level number rather than by the level's "
                "text, so the key stays meaningful when two levels read similarly. `confidence` "
                "says how concentrated that distribution was — a row spread evenly across the whole "
                "scale lands mid-scale with a low confidence, which is a different fact from a row "
                "the model is sure belongs in the middle.\n\n"
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
                ("Rate every row of a table via LATERAL", _LATERAL_EXAMPLE),
                ("Rate a single literal and keep the whole distribution", _LITERAL_EXAMPLE),
            ),
        )
        examples = [
            FunctionExample(sql=_LATERAL_EXAMPLE, description="Rate every row of a table via LATERAL"),
            FunctionExample(sql=_LITERAL_EXAMPLE, description="Rate a single literal and keep the whole distribution"),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[ScoreArgs]) -> BindResponse:
        """Reject an incomplete or malformed scale at plan time, before any row is billed."""
        question_of(params.args.instructions, params.args.criteria)
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def process(
        cls,
        params: ProcessParams[ScoreArgs],
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

        answers = [None if r is None else r.answers[QUESTION_ID] for r in responses]
        columns: dict[str, list[Any]] = {
            "score": [a["score"] if a is not None else None for a in answers],
            "confidence": [a["confidence"] if a is not None else None for a in answers],
            "probabilities": [list(a["probabilities"].items()) if a is not None else None for a in answers],
            "model": [r.model if r else None for r in responses],
            "input_tokens": [r.input_tokens if r else None for r in responses],
            "output_tokens": [r.output_tokens if r else None for r in responses],
        }
        full = pa.RecordBatch.from_pydict(columns, schema=cls.FIXED_SCHEMA)
        out.emit(full.select(params.output_schema.names))
