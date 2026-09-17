# Copyright 2026 Query Farm LLC - https://query.farm

"""``choice()`` — a TypeSafe choice question as a LATERAL-joinable table function.

It is a **blended** (:class:`~vgi.table_in_out_function.RowTransformFunction`)
table-in-out function: its positional argument *is* the per-row input column, so
one registration serves a literal call and a correlated LATERAL alike::

    SELECT * FROM typesafe.main.choice(
        'My package never arrived',
        instructions => 'Which team should handle this?',
        criteria => MAP {'shipping': 'Delivery status, delays, lost packages',
                         'billing':  'Charges, invoices, payment problems'});

    SELECT t.id, c.choice, c.confidence
    FROM tickets t,
         LATERAL typesafe.main.choice(t.body,
             instructions => 'Which team should handle this?',
             criteria => MAP {'shipping': '...', 'billing': '...'}) c;

The blended contract shapes the signature:

* ``state`` is positional, so it is read off the input batch and is *not* on
  ``params.args``.
* The question itself (``instructions``, ``criteria``) is the same for every row
  and is therefore a set of *named*, bind-time arguments — a positional constant
  is rejected for blended functions.
* No ``finalize`` — DuckDB forbids it under correlated LATERAL.

The map is strictly 1->1: every input row yields exactly one output row, and a
NULL state yields a row of NULLs without a request. Keeping row counts equal is
what lets the engine pair outputs with their correlated rows without provenance.
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

CHOICE_SCHEMA = pa.schema(
    [
        field("choice", pa.string(), "The highest-probability option; NULL when the input state was NULL."),
        field("confidence", pa.float64(), "0-1 score of how concentrated the probability distribution is."),
        field(
            "probabilities",
            pa.map_(pa.string(), pa.float64()),
            "Probability of every option, in the order the criteria listed them; sums to 1.",
        ),
        field("model", pa.string(), "The model that answered, as reported by the API."),
        field("input_tokens", pa.int64(), "Input tokens billed for this row's request."),
        field("output_tokens", pa.int64(), "Output tokens billed for this row's request."),
    ]
)

_LATERAL_EXAMPLE = (
    "SELECT t.body, c.choice, c.confidence "
    "FROM (VALUES ('My package never arrived'), ('I was charged twice')) t(body), "
    "LATERAL typesafe.main.choice(t.body, "
    "instructions => 'Which team should handle this?', "
    "criteria => MAP {'shipping': 'Delivery status, delays, lost packages', "
    "'billing': 'Charges, invoices, payment problems'}) c"
)
_LITERAL_EXAMPLE = (
    "SELECT choice, confidence FROM typesafe.main.choice('I was charged twice', "
    "instructions => 'Which team should handle this?', "
    "criteria => MAP {'shipping': 'Delivery status, delays, lost packages', "
    "'billing': 'Charges, invoices, payment problems'})"
)


@dataclass(slots=True, frozen=True, kw_only=True)
class ChoiceArgs:
    """``choice(state, instructions =>, criteria =>)``."""

    state: Annotated[str, Arg(0, doc="The content to evaluate — the per-row input column")]
    # Empty string means "not supplied": a `str | None` annotation resolves to
    # the Arrow null type, which DuckDB cannot cast a VARCHAR into. Both are
    # required, and on_bind says so by name rather than leaving it to the API.
    instructions: Annotated[
        str, Arg("instructions", doc="The question to answer about each state (required)", default="")
    ] = ""
    # Optional only so that omitting it reaches on_bind's explanation instead of
    # the framework's generic NULL rejection. The explicit arrow_type keeps the
    # SQL signature a MAP; without it `| None` would resolve to the Arrow null type.
    criteria: Annotated[
        dict[str, str] | None,
        Arg(
            "criteria",
            arrow_type=pa.map_(pa.string(), pa.string()),
            doc="Each option paired with a description of when it applies (required)",
            default=None,
        ),
    ] = None
    model: Annotated[str, Arg("model", doc="TypeSafe model id", default=api.DEFAULT_MODEL)] = (
        api.DEFAULT_MODEL
    )
    concurrency: Annotated[
        int,
        Arg("concurrency", doc="Max in-flight API requests per input batch", default=8, ge=1, le=64),
    ] = 8


def criteria_of(raw: Any) -> dict[str, str]:
    """Normalise the ``criteria`` MAP argument into ``{option: description}``.

    An Arrow map scalar converts to a list of ``(key, value)`` pairs; a dict is
    accepted too so the function can be driven directly from Python.

    Raises:
        ValueError: The map is missing, empty, too large, or has a blank option.
    """
    if raw is None:
        pairs: list[tuple[Any, Any]] = []
    elif isinstance(raw, dict):
        pairs = list(raw.items())
    else:
        pairs = [(p["key"], p["value"]) if isinstance(p, dict) else tuple(p) for p in raw]
    if not pairs:
        raise ValueError(
            "choice() requires 'criteria': a MAP of option -> description, "
            "e.g. criteria => MAP {'spam': 'Unsolicited bulk mail', 'ham': 'Everything else'}"
        )
    if len(pairs) > api.MAX_OPTIONS:
        raise ValueError(f"choice() accepts at most {api.MAX_OPTIONS} options, got {len(pairs)}")
    criteria: dict[str, str] = {}
    for option, description in pairs:
        if option is None or not str(option).strip():
            raise ValueError("choice() criteria has a NULL or blank option name")
        criteria[str(option)] = "" if description is None else str(description)
    return criteria


class ChoiceFunction(RowTransformFunction[ChoiceArgs]):
    """Pick one option per input row — strictly 1->1."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = CHOICE_SCHEMA

    class Meta:
        """Catalog metadata: name, docs, and the examples clients copy."""

        name = "choice"
        description = "Classify each row into one of a set of options with a TypeSafe choice question"
        categories = ["classification", "blended"]
        required_secrets = [SecretLookupEntry(secret_type=auth.SECRET_TYPE)]
        tags = docs(
            category="classification",
            result_schema=CHOICE_SCHEMA,
            llm=(
                "Classify text into exactly one of a fixed set of options using TypeSafe's System "
                "One model. Pass the text as the positional argument — a literal, or a column under "
                "LATERAL to classify a whole table — and describe the question once with the named "
                "`instructions` and `criteria` arguments. Returns one row per input row: the chosen "
                "option, a 0-1 confidence, and the full probability distribution as a `MAP`. Filter "
                "on `confidence` to route uncertain rows to review. Needs a `typesafe` secret."
            ),
            md=(
                "One TypeSafe choice question, asked of every input row.\n\n"
                "### Arguments\n\n"
                "- `state` (positional): the content to evaluate. A literal, or a column.\n"
                "- `instructions =>`: the question, e.g. `'Which team should handle this?'`.\n"
                "- `criteria =>`: a `MAP` from option name to a description of when it applies. "
                "Up to 255 options; include an `'other'` option when the list may be incomplete.\n"
                "- `model =>`: defaults to `jev-latest`.\n"
                "- `concurrency =>`: in-flight requests per batch (default 8).\n\n"
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
                ("Classify every row of a table via LATERAL", _LATERAL_EXAMPLE),
                ("Classify a single literal", _LITERAL_EXAMPLE),
            ),
        )
        examples = [
            FunctionExample(sql=_LATERAL_EXAMPLE, description="Classify every row of a table via LATERAL"),
            FunctionExample(sql=_LITERAL_EXAMPLE, description="Classify a single literal"),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[ChoiceArgs]) -> BindResponse:
        """Reject an incomplete question at plan time, before any row is billed."""
        if not params.args.instructions.strip():
            raise ValueError(
                "choice() requires 'instructions', e.g. instructions => 'Which team should handle this?'"
            )
        criteria_of(params.args.criteria)
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def process(
        cls,
        params: ProcessParams[ChoiceArgs],
        state: None,
        batch: pa.RecordBatch,
        out: OutputCollector,
    ) -> None:
        """Answer the question for each row of one input batch."""
        states = [None if s is None else str(s) for s in batch.column("state").to_pylist()]
        answers: list[api.ChoiceAnswer | None]
        if any(s is not None for s in states):
            # Resolved only when there is something to ask, so an all-NULL
            # batch succeeds without a key.
            credentials = auth.for_call(params.secrets)
            with api.open_client() as client:
                answers = api.ask_choices(
                    states,
                    instructions=params.args.instructions,
                    criteria=criteria_of(params.args.criteria),
                    credentials=credentials,
                    client=client,
                    model=params.args.model or api.DEFAULT_MODEL,
                    concurrency=params.args.concurrency,
                )
        else:
            answers = [None] * len(states)

        columns: dict[str, list[Any]] = {
            "choice": [a.choice if a else None for a in answers],
            "confidence": [a.confidence if a else None for a in answers],
            "probabilities": [list(a.probabilities.items()) if a else None for a in answers],
            "model": [a.model if a else None for a in answers],
            "input_tokens": [a.input_tokens if a else None for a in answers],
            "output_tokens": [a.output_tokens if a else None for a in answers],
        }
        full = pa.RecordBatch.from_pydict(columns, schema=cls.FIXED_SCHEMA)
        out.emit(full.select(params.output_schema.names))
