# Copyright 2026 Query Farm LLC - https://query.farm

"""``is_true()`` — a noul question as a scalar expression.

Why a scalar at all
-------------------
Every other function in this worker is a table function, because every other
answer has several parts: a choice has an option, a confidence and a
distribution; a score has a position, a confidence and a distribution. A **noul
answer is one number**. Wrapping one number in a one-column table costs the
caller a join and buys nothing back, so this is the one question type a scalar
can express without loss.

What that buys is *conciseness*, and only conciseness. A scalar drops straight
into the places SQL expects an expression — a ``WHERE`` predicate, a ``CASE``
arm, an ``ORDER BY`` key, the right-hand side of ``UPDATE ... SET`` — where the
table-function form needs a LATERAL join or a correlated scalar subquery around
it. Both were tried before this function was written, and both work: ``noul()``
can already express all of those. Nothing here is newly *possible*. What changes
is that the query stops being about the plumbing.

The trade is real and runs the other way too. A scalar has no named arguments in
DuckDB, so this one takes exactly the state and the question, uses the default
model, and reports no token usage — ``noul()`` is where ``criteria =>``,
``model =>``, ``concurrency =>`` and the usage columns live.

It still batches
----------------
A VGI scalar is handed an Arrow vector, not a row, so this makes one API request
per **distinct non-null state** in the chunk — the same accounting ``noul()``
does, through the same :func:`vgi_typesafe.typesafe_api.ask_many`. There is no
second request path here, and no per-row loop. A NULL input yields NULL and
costs nothing; an API failure raises rather than degrading to NULL, which would
be indistinguishable from that NULL input.

The name
--------
``is_true`` names the question — "is this true of the row?" — not the return
type: the value is the *probability* that the answer is yes, so it is compared
against a threshold rather than used as a bare predicate. Every example says so,
because the alternative reading (a BOOLEAN) is the one mistake this name invites.
"""

from __future__ import annotations

from typing import Annotated, Any

import pyarrow as pa
from vgi.arguments import ConstParam, Param, Returns, Secret, SecretLookupEntry
from vgi.metadata import FunctionExample
from vgi.scalar_function import BindParameters, BindResult, ScalarFunction

from vgi_typesafe import auth
from vgi_typesafe import typesafe_api as api
from vgi_typesafe.meta import docs, examples

#: The name this function gives its single question in each request.
QUESTION_ID = "noul"

#: ``instructions`` is the only constant argument, so it is index 0 of the
#: bind-time scalars (columns are delivered separately, as the input batch).
INSTRUCTIONS_INDEX = 0

_QUESTION = "'Is this message about a lost package?'"
_WHERE_EXAMPLE = (
    "SELECT t.body FROM (VALUES ('My package never arrived'), ('Thanks for the quick refund')) t(body) "
    f"WHERE typesafe.main.is_true(t.body, {_QUESTION}) > 0.5"
)
_SELECT_EXAMPLE = f"SELECT typesafe.main.is_true('My package never arrived', {_QUESTION}) AS lost_package"


def instructions_of(raw: Any) -> str:
    """Validate the question argument, which is constant for the whole call.

    Args:
        raw: The ``instructions`` argument as DuckDB folded it, or None when the
            caller passed SQL NULL.

    Returns:
        The question, unchanged — TypeSafe sees exactly what the caller wrote.

    Raises:
        ValueError: The question is missing or blank, which the API would answer
            with a bare 422 that does not say what to write instead.
    """
    question = "" if raw is None else str(raw)
    if not question.strip():
        raise ValueError(
            "is_true() needs a question as its second argument, e.g. is_true(body, 'Does this need a reply today?')"
        )
    return question


def probabilities(
    states: pa.StringArray,
    *,
    instructions: str,
    secret: dict[str, Any] | None,
) -> pa.DoubleArray:
    """Answer one yes/no question about every row of a chunk.

    Args:
        states: The per-row content to evaluate; a NULL element is not asked about.
        instructions: The question, the same for every row.
        secret: The resolved ``typesafe`` secret's fields, or None to fall back
            to the environment.

    Returns:
        One probability per input row, NULL where the input was NULL.
    """
    values: list[str | None] = states.to_pylist()
    if not any(value is not None for value in values):
        # Nothing to ask, so nothing should require credentials.
        return pa.array([None] * len(values), type=pa.float64())
    credentials = auth.for_call({auth.SECRET_TYPE: secret} if secret else None)
    question = {"type": "noul", "instructions": instructions}
    with api.open_client() as client:
        # One request per DISTINCT non-null state, concurrently — ask_many is
        # the worker's only batching path, and this rides it unchanged.
        responses = api.ask_many(
            values,
            {QUESTION_ID: question},
            credentials=credentials,
            client=client,
        )
    answered = [None if r is None else r.answers[QUESTION_ID]["noul"] for r in responses]
    return pa.array(answered, type=pa.float64())


class IsTrueFunction(ScalarFunction):
    """``is_true(state, instructions)`` — the probability that a yes/no question is yes."""

    class Meta:
        """Catalog metadata: name, docs, and the examples clients copy."""

        name = "is_true"
        description = (
            "Probability that a yes/no question is true of the given content, as a plain expression "
            "usable in WHERE, CASE and ORDER BY"
        )
        categories = ["classification", "scalar"]
        required_secrets = [SecretLookupEntry(secret_type=auth.SECRET_TYPE)]
        tags = docs(
            category="classification",
            llm=(
                "Ask TypeSafe one yes/no question about a value and get back a single `DOUBLE`: the "
                "probability that the answer is yes, where near 1 is yes, near 0 is no and 0.5 is "
                "genuinely undecided. It is a scalar, so it goes wherever an expression goes — a "
                "`WHERE` predicate, a `CASE` arm, an `ORDER BY` key, the right-hand side of an "
                "`UPDATE`. Compare it against a threshold; it is a probability, not a boolean. Take "
                "the first argument from a column to judge a whole table. One request per distinct "
                "non-null value, and a NULL value costs nothing and answers NULL. Use `noul()` "
                "instead when you need `criteria`, a specific model, or the token usage, and `ask()` "
                "when one row needs several questions at once. Needs a `typesafe` secret."
            ),
            md=(
                "One yes/no question, answered as a number you can put in an expression.\n\n"
                "### Arguments\n\n"
                "1. the content to evaluate — a literal, or a column.\n"
                "2. the question, constant for the whole query and checked when it is planned.\n\n"
                "A scalar takes no named arguments in DuckDB, so that is the whole signature: the "
                "default model, no criteria, no usage columns. `noul()` is the same question with "
                "all of those, in table-function form.\n\n"
                "### Reading the answer\n\n"
                "The result is the probability that the answer is yes: near 1 is yes, near 0 is no, "
                "and 0.5 is the model saying the content does not decide the question. So it is "
                "compared against a threshold rather than used as a bare predicate — see this "
                "function's example queries for the shape. Pick the threshold to suit the cost of "
                "being wrong, and keep the rows near 0.5 for a human.\n\n"
                "### Why it exists\n\n"
                "A noul answer is one number, so a scalar loses nothing by returning just that, and "
                "the query stops needing a join to reach it. `noul()` can express the same filters "
                "and orderings through a `LATERAL` join or a scalar subquery — this is shorter, not "
                "more capable.\n\n"
                "### Cost and row semantics\n\n"
                "One API request per distinct non-null value in each chunk, issued concurrently; "
                "repeated values are asked once. A NULL value answers NULL and makes no request. An "
                "API failure raises rather than becoming NULL, which would be indistinguishable "
                "from that NULL input. 429 and 529 are retried with backoff first.\n\n"
                "### Authentication\n\n"
                "Add `base_url` to the secret to target the bundled mock endpoint. "
                "`TYPESAFE_API_KEY` in the worker's environment is the fallback.\n\n"
                "```sql\n"
                "CREATE SECRET (TYPE typesafe, api_key '...');\n"
                "```"
            ),
            example_queries=examples(
                ("Keep only the rows a yes/no question is true of", _WHERE_EXAMPLE),
                ("Ask about one literal", _SELECT_EXAMPLE),
            ),
        )
        examples = [
            FunctionExample(sql=_WHERE_EXAMPLE, description="Keep only the rows a yes/no question is true of"),
            FunctionExample(sql=_SELECT_EXAMPLE, description="Ask about one literal"),
        ]

    @classmethod
    def on_bind(cls, params: BindParameters) -> BindResult:
        """Reject a blank question at plan time, before any row is billed."""
        instructions_of(params.constant_arguments.get(INSTRUCTIONS_INDEX, default=None))
        return BindResult(output_type=cls.output_type(params))

    @classmethod
    def compute(
        cls,
        state: Annotated[pa.StringArray, Param(doc="The content to evaluate — a literal, or a column")],
        instructions: Annotated[str, ConstParam(doc="The yes/no question asked about every row")],
        typesafe_secret: Annotated[dict[str, pa.Scalar[Any]] | None, Secret(auth.SECRET_TYPE)] = None,
    ) -> Annotated[pa.DoubleArray, Returns()]:
        """Answer the question for every row of one chunk.

        Args:
            state: The per-row content to evaluate.
            instructions: The question, constant for the whole call.
            typesafe_secret: The resolved ``typesafe`` secret's fields, or None.

        Returns:
            One probability per input row, NULL where the input was NULL.
        """
        return probabilities(
            state,
            instructions=instructions_of(instructions),
            secret=dict(typesafe_secret) if typesafe_secret else None,
        )
