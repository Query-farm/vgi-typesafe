# Copyright 2026 Query Farm LLC - https://query.farm

"""``ask()`` — several TypeSafe questions about each row, in one request per row.

A System One request carries one ``state`` and a map of named questions that the
API answers in parallel, so asking five things about a row costs the same single
request as asking one. ``ask()`` is that request as a LATERAL-joinable table
function::

    SELECT t.id, a.dept.choice, a.urgent.noul, a.severity.score
    FROM tickets t,
         LATERAL typesafe.main.ask(t, questions => {
             'dept':     {'type': 'choice', 'instructions': 'Which team should handle this?',
                          'criteria': {'shipping': 'Delays, lost packages', 'billing': 'Charges, invoices'}},
             'urgent':   {'type': 'noul',   'instructions': 'Does this need a reply today?'},
             'severity': {'type': 'score',  'instructions': 'How bad is it?',
                          'criteria': ['minor', 'disruptive', 'critical']}}) a;

Why ``questions`` is a struct keyed by question name
----------------------------------------------------
The shapes differ per type — a choice's criteria is a map, a score's is an
ordered list, a noul's is optional — so the questions cannot share a LIST (DuckDB
must unify a list's element type, and cannot) and a LIST of UNIONs does not
type-check as a literal either. As fields of one struct each question keeps its
own type, and its key doubles as the output column name. The argument is
therefore declared ANY and validated here, at bind, with errors that name the
question at fault. A JSON string and a MAP are accepted too.

State
-----
``state`` is declared ANY as well, so it arrives with whatever type the call
resolved: a VARCHAR is sent as a string; a STRUCT (including a whole row,
``ask(t, ...)``), LIST or MAP is sent as the equivalent JSON object or array. A
DuckDB JSON column reaches the worker as plain text, indistinguishable from
VARCHAR, so parsing it is opt-in: ``parse_json => true``.

Output
------
One column per question, named after it — a STRUCT whose fields depend on the
question's type — plus a trailing ``usage`` struct. Strictly one output row per
input row; a NULL state yields NULL answers and makes no request.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any

import pyarrow as pa
from vgi.arguments import AnyArrow, Arg, SecretLookupEntry
from vgi.invocation import BindResponse
from vgi.metadata import FunctionExample
from vgi.table_function import BindParams, ProcessParams
from vgi.table_in_out_function import RowTransformFunction
from vgi_rpc.rpc import OutputCollector

from vgi_typesafe import auth
from vgi_typesafe import typesafe_api as api
from vgi_typesafe.meta import docs, examples, field

#: The trailing output column; a question may not take its name.
USAGE_COLUMN = "usage"

ANSWER_TYPES: dict[str, pa.DataType] = {
    "choice": pa.struct(
        [
            pa.field("choice", pa.string()),
            pa.field("confidence", pa.float64()),
            pa.field("probabilities", pa.map_(pa.string(), pa.float64())),
        ]
    ),
    "noul": pa.struct([pa.field("noul", pa.float64())]),
    "score": pa.struct(
        [
            pa.field("score", pa.float64()),
            pa.field("confidence", pa.float64()),
            # Keyed by level number (0 = the first criterion), not by its text.
            pa.field("probabilities", pa.map_(pa.int32(), pa.float64())),
        ]
    ),
}
USAGE_TYPE = pa.struct(
    [
        pa.field("model", pa.string()),
        pa.field("input_tokens", pa.int64()),
        pa.field("output_tokens", pa.int64()),
    ]
)

_EXAMPLE_QUESTIONS = (
    "questions => {"
    "'dept': {'type': 'choice', 'instructions': 'Which team should handle this?', "
    "'criteria': {'shipping': 'Delivery status, delays, lost packages', "
    "'billing': 'Charges, invoices, payment problems'}}, "
    "'urgent': {'type': 'noul', 'instructions': 'Is the customer angry?', "
    "'criteria': {'true': 'angry, furious, unacceptable', 'false': 'calm, thanks'}}, "
    "'severity': {'type': 'score', 'instructions': 'How bad is it?', "
    "'criteria': ['minor question', 'disruptive delay', 'critical outage']}}"
)
_ROW_EXAMPLE = (
    "SELECT t.id, a.dept.choice AS dept, a.urgent.noul AS urgent, a.severity.score AS severity "
    "FROM (VALUES (1, 'My package is lost, this is unacceptable'), (2, 'Thanks, one invoice question')) "
    f"t(id, body), LATERAL typesafe.main.ask(t, {_EXAMPLE_QUESTIONS}) a ORDER BY t.id"
)
_LITERAL_EXAMPLE = (
    f"SELECT dept.choice, urgent.noul FROM typesafe.main.ask('I was charged twice', {_EXAMPLE_QUESTIONS})"
)


@dataclass(slots=True, frozen=True, kw_only=True)
class AskArgs:
    """``ask(state, questions => ...)``."""

    state: Annotated[AnyArrow, Arg(0, doc="The content to evaluate — a piece of text, a row, or a structured value")]
    questions: Annotated[
        AnyArrow,
        Arg(
            "questions",
            doc=(
                "One entry per question, keyed by the name you want its output column to have. "
                "Each entry carries the question type, its instructions, and (for most types) its "
                "criteria; see this function's documentation for the three types and the shape "
                "each one expects."
            ),
        ),
    ]
    model: Annotated[str, Arg("model", doc="TypeSafe model id", default=api.DEFAULT_MODEL)] = api.DEFAULT_MODEL
    concurrency: Annotated[
        int,
        Arg("concurrency", doc="Max in-flight API requests per input batch", default=8, ge=1, le=64),
    ] = 8
    parse_json: Annotated[
        bool,
        Arg("parse_json", doc="Parse a textual state as JSON and send it structured", default=False),
    ] = False


# ---------------------------------------------------------------------------
# questions
# ---------------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """An argument value as plain JSON-shaped Python.

    A struct literal arrives as a dict and a MAP as a list of ``(key, value)``
    pairs. NULLs are dropped: unifying differently-shaped structs (as a MAP of
    questions must) pads the missing fields with NULL, which is not a value the
    caller wrote.
    """
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        if value and all(isinstance(item, tuple) and len(item) == 2 for item in value):
            return {str(k): _plain(v) for k, v in value if v is not None}
        return [_plain(item) for item in value]
    return value


def _question(name: str, raw: Any, origin: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{origin}: question {name!r} must be a struct with 'type' and 'instructions'")
    unknown = set(raw) - {"type", "instructions", "criteria"}
    if unknown:
        raise ValueError(
            f"{origin}: question {name!r} has unknown field(s) {', '.join(sorted(unknown))}; "
            "expected 'type', 'instructions', 'criteria'"
        )
    kind = str(raw.get("type") or "").strip().lower()
    if kind not in api.QUESTION_TYPES:
        raise ValueError(
            f"{origin}: question {name!r} has type {raw.get('type')!r}; expected one of {', '.join(api.QUESTION_TYPES)}"
        )
    # TypeSafe accepts a string, an object or an array here, for every question
    # type — a structured instruction is how you give the model a rubric rather
    # than a sentence. Only emptiness is an error.
    instructions = raw.get("instructions")
    if instructions is None or (isinstance(instructions, str) and not instructions.strip()):
        raise ValueError(f"{origin}: question {name!r} requires 'instructions'")
    question: dict[str, Any] = {"type": kind, "instructions": instructions}
    criteria = raw.get("criteria")

    if kind == "choice":
        if not isinstance(criteria, dict) or not criteria:
            raise ValueError(
                f"{origin}: choice question {name!r} requires 'criteria': a struct or MAP of option -> description"
            )
        if len(criteria) > api.MAX_OPTIONS:
            raise ValueError(f"{origin}: choice question {name!r} has more than {api.MAX_OPTIONS} options")
        if any(not option.strip() for option in criteria):
            raise ValueError(f"{origin}: choice question {name!r} has a blank option name")
        question["criteria"] = criteria
    elif kind == "score":
        levels = api.MIN_SCORE_LEVELS, api.MAX_SCORE_LEVELS
        if not isinstance(criteria, list) or not levels[0] <= len(criteria) <= levels[1]:
            raise ValueError(
                f"{origin}: score question {name!r} requires 'criteria': an ordered list of "
                f"{levels[0]}-{levels[1]} level descriptions, lowest first"
            )
        question["criteria"] = criteria
    elif criteria is not None:
        if not isinstance(criteria, dict) or set(criteria) - {"true", "false"}:
            raise ValueError(
                f"{origin}: noul question {name!r} takes optional 'criteria' with only 'true' and 'false' keys"
            )
        if criteria:
            question["criteria"] = criteria
    return question


def questions_of(raw: Any, *, origin: str = "ask()", reserved: str | None = USAGE_COLUMN) -> dict[str, dict[str, Any]]:
    """Normalise and validate a ``questions`` value.

    Args:
        raw: The value as DuckDB delivered it — a struct literal, a MAP's
            ``(key, value)`` pairs, a JSON string, or an ANY-typed wrapper
            around any of those.
        origin: The function name every message is prefixed with. ``ask()`` and
            ``ask_dynamic()`` share these rules, so they share the messages, and
            a user must still be told which one they called.
        reserved: A name no question may take because the function already
            spends it on an output column, or None when it spends none.

    Returns:
        Each question keyed by name, in the order given, with its type
        lowercased and its criteria in the shape the API expects.

    Raises:
        ValueError: With a message naming the question at fault.
    """
    value = raw.value if hasattr(raw, "value") else raw
    value = value.as_py() if hasattr(value, "as_py") else value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{origin}: 'questions' is a string but not valid JSON: {exc}") from exc
    value = _plain(value)
    if not isinstance(value, dict) or not value:
        raise ValueError(
            f"{origin} requires 'questions': a struct keyed by question name, e.g. "
            "{'spam': {'type': 'noul', 'instructions': 'Is this spam?'}}"
        )
    questions: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for name, body in value.items():
        if not name.strip():
            raise ValueError(f"{origin}: a question has a blank name")
        # Question names become column names, which DuckDB compares case-insensitively.
        folded = name.lower()
        if reserved is not None and folded == reserved:
            raise ValueError(f"{origin}: a question may not be named {reserved!r}; that output column is taken")
        if folded in seen:
            raise ValueError(f"{origin}: question name {name!r} is repeated (names are case-insensitive)")
        seen.add(folded)
        questions[name] = _question(name, body, origin)
    return questions


def output_schema_of(questions: Mapping[str, Mapping[str, Any]]) -> pa.Schema:
    """One answer STRUCT per question — commented with what it asks — plus usage."""
    fields = [
        field(name, ANSWER_TYPES[q["type"]], f"{q['type']}: {q['instructions']}") for name, q in questions.items()
    ]
    fields.append(field(USAGE_COLUMN, USAGE_TYPE, "Model and billed tokens for this row's request."))
    return pa.schema(fields)


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


def _is_structured(kind: pa.DataType) -> bool:
    return pa.types.is_struct(kind) or pa.types.is_list(kind) or pa.types.is_large_list(kind) or pa.types.is_map(kind)


def _is_text(kind: pa.DataType) -> bool:
    return pa.types.is_string(kind) or pa.types.is_large_string(kind)


def check_state_type(kind: pa.DataType, *, parse_json: bool, origin: str = "ask()") -> None:
    """Reject a state column the API has no representation for, at bind."""
    if parse_json and not _is_text(kind):
        raise ValueError(f"{origin}: parse_json => true needs a VARCHAR or JSON state, not {kind}")
    if not (_is_text(kind) or _is_structured(kind)):
        raise ValueError(
            f"{origin}: state must be VARCHAR, STRUCT, LIST or MAP, not {kind}; "
            "cast it to VARCHAR, or wrap it in a struct such as {'value': x}"
        )


def _scalar_to_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        raise ValueError("ask(): state contains a BLOB; TypeSafe accepts text only — drop or encode that field")
    return str(value)


def to_json(value: Any, kind: pa.DataType) -> Any:
    """One Arrow value as JSON-shaped Python, driven by its type.

    Type-driven rather than value-driven because a MAP and a LIST of pairs look
    identical once converted to Python, and an empty one looks like nothing at all.
    """
    if value is None:
        return None
    if pa.types.is_struct(kind):
        return {f.name: to_json(value.get(f.name), f.type) for f in kind}
    if pa.types.is_map(kind):
        return {str(_scalar_to_json(k)): to_json(v, kind.item_type) for k, v in value}
    if pa.types.is_list(kind) or pa.types.is_large_list(kind) or pa.types.is_fixed_size_list(kind):
        return [to_json(item, kind.value_type) for item in value]
    return _scalar_to_json(value)


def _parse_json_state(text: str) -> Any:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ask(): parse_json => true, but a state is not valid JSON ({exc}): {text[:80]!r}") from exc
    if not isinstance(parsed, (dict, list, str)):
        raise ValueError(
            f"ask(): a JSON state must be an object, array or string, not {type(parsed).__name__}: {text[:80]!r}"
        )
    return parsed


def _has_content(value: Any) -> bool:
    """Whether a structured state holds anything at all to evaluate."""
    if value is None:
        return False
    if isinstance(value, dict):
        return any(_has_content(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_content(v) for v in value)
    return True


def states_of(column: pa.Array | pa.ChunkedArray, *, parse_json: bool) -> list[Any]:  # type: ignore[type-arg]
    """The request ``state`` for every row of the input column; ``None`` means "do not ask".

    A structured state with no non-null content — ``{'message': NULL, 'tier':
    NULL}``, an empty list — is treated as NULL. The struct itself is not NULL in
    SQL, but there is nothing in it to judge, and asking anyway would bill a
    request for a meaningless answer.
    """
    values = column.to_pylist()
    if _is_text(column.type):
        if not parse_json:
            return values
        states = [None if v is None else _parse_json_state(v) for v in values]
    else:
        states = [to_json(v, column.type) for v in values]
    return [s if _has_content(s) else None for s in states]


# ---------------------------------------------------------------------------
# the function
# ---------------------------------------------------------------------------


def _answer_cell(answer: dict[str, Any]) -> dict[str, Any]:
    """An answer as pyarrow wants it: a MAP field is a list of pairs, not a dict."""
    if "probabilities" in answer:
        return {**answer, "probabilities": list(answer["probabilities"].items())}
    return answer


class AskFunction(RowTransformFunction[AskArgs]):
    """Ask several questions about each input row — strictly 1->1, one request per row."""

    class Meta:
        """Catalog metadata: name, docs, and the examples clients copy."""

        name = "ask"
        description = "Ask several TypeSafe questions (choice, noul, score) about each row in one request"
        categories = ["classification", "scoring", "blended"]
        required_secrets = [SecretLookupEntry(secret_type=auth.SECRET_TYPE)]
        tags = docs(
            category="classification",
            llm=(
                "Ask any number of TypeSafe questions about each row and get one typed `STRUCT` column "
                "per question, for the cost of a single API request per row. Three question types: "
                "`choice` picks one option (-> choice, confidence, probabilities), `noul` is a yes/no "
                "probability (-> noul, 0-1), `score` places the row on an ordered scale (-> score, "
                "confidence, probabilities by level). Pass the content as the positional argument — a "
                "string, a struct, or the whole row alias under LATERAL — and the questions as a "
                "struct keyed by the column name you want. Prefer this over several `choice()` calls."
            ),
            md=(
                "Several questions about each row, answered in one request.\n\n"
                "### State (positional)\n\n"
                "- `VARCHAR` is sent as a string.\n"
                "- `STRUCT`, `LIST` and `MAP` are sent as the equivalent JSON object or array. The "
                "row alias itself works: `ask(t, ...)` sends every column of `t`.\n"
                "- A DuckDB `JSON` column is plain text to the worker; pass `parse_json => true` to "
                "send it structured.\n\n"
                "TypeSafe recommends an object for most requests: descriptive field names tell the "
                "model how the parts relate.\n\n"
                "### Questions (`questions =>`)\n\n"
                "A struct keyed by question name. Each value has `type`, `instructions`, and:\n\n"
                "- `choice` — `criteria`: struct or `MAP` of option -> description (1-255 options).\n"
                "- `score` — `criteria`: ordered list of 2-10 level descriptions, lowest first.\n"
                "- `noul` — optional `criteria` with `'true'` and/or `'false'` descriptions.\n\n"
                "`instructions` and every criterion may be a plain string or a structured object — "
                "TypeSafe accepts both, and a rubric (`{what, not_for, examples}` for a choice "
                "option, `{summary, signals}` for a score level) is what separates options a model "
                "keeps confusing. A JSON string or a `MAP` is accepted in place of the struct. "
                "Questions are validated at bind, before any row is sent.\n\n"
                "### Output\n\n"
                "One `STRUCT` column per question, named after it:\n\n"
                "| Type | Fields |\n| --- | --- |\n"
                "| choice | `choice VARCHAR`, `confidence DOUBLE`, `probabilities MAP(VARCHAR, DOUBLE)` |\n"
                "| noul | `noul DOUBLE` — near 1 is yes, near 0 is no, 0.5 is genuinely undecided |\n"
                "| score | `score DOUBLE`, `confidence DOUBLE`, `probabilities MAP(INTEGER, DOUBLE)` "
                "keyed by level (0 = first) |\n\n"
                "then `usage STRUCT(model, input_tokens, output_tokens)`. Strictly one output row per "
                "input row. A NULL state — or a struct/list with no non-null content — yields NULL "
                "answers and makes no request. Identical states "
                "in a batch are asked once. API errors raise — they never become NULL."
            ),
            example_queries=examples(
                ("Route, flag and grade every row of a table in one pass", _ROW_EXAMPLE),
                ("Ask several questions about a single literal", _LITERAL_EXAMPLE),
            ),
            extra={
                "vgi.result_dynamic_columns_md": (
                    "The result carries one column per entry in `questions`, named after that "
                    "entry, followed by a `usage` column. A question column's type is decided by "
                    "its `type` field, so there are three variants.\n\n"
                    "### A `choice` question\n\n"
                    "| Name | Type | Description |\n"
                    "| --- | --- | --- |\n"
                    "| &lt;question name&gt; | STRUCT(choice VARCHAR, confidence DOUBLE, "
                    "probabilities MAP(VARCHAR, DOUBLE)) | The chosen option, how concentrated "
                    "the distribution was, and every option's probability in criteria order. |\n\n"
                    "### A `noul` question\n\n"
                    "| Name | Type | Description |\n"
                    "| --- | --- | --- |\n"
                    "| &lt;question name&gt; | STRUCT(noul DOUBLE) | Probability between 0 and 1 "
                    "that the answer is yes; 0.5 is genuinely undecided. |\n\n"
                    "### A `score` question\n\n"
                    "| Name | Type | Description |\n"
                    "| --- | --- | --- |\n"
                    "| &lt;question name&gt; | STRUCT(score DOUBLE, confidence DOUBLE, "
                    "probabilities MAP(INTEGER, DOUBLE)) | Probability-weighted position on the "
                    "scale, its confidence, and each level's probability keyed by level number "
                    "(0 is the first criterion). |\n\n"
                    "### Always present\n\n"
                    "| Name | Type | Description |\n"
                    "| --- | --- | --- |\n"
                    "| usage | STRUCT(model VARCHAR, input_tokens BIGINT, output_tokens BIGINT) | "
                    "The model that answered and the tokens billed for this row's request. |"
                ),
            },
        )
        examples = [
            FunctionExample(sql=_ROW_EXAMPLE, description="Route, flag and grade every row of a table in one pass"),
            FunctionExample(sql=_LITERAL_EXAMPLE, description="Ask several questions about a single literal"),
        ]

    @classmethod
    def on_bind(cls, params: BindParams[AskArgs]) -> BindResponse:
        """Validate the questions and the state type, and shape the output from the questions."""
        questions = questions_of(params.args.questions)
        input_schema = params.bind_call.input_schema
        if input_schema is not None and len(input_schema) > 0:
            check_state_type(input_schema.field(0).type, parse_json=params.args.parse_json)
        return BindResponse(output_schema=output_schema_of(questions))

    @classmethod
    def process(
        cls,
        params: ProcessParams[AskArgs],
        state: None,
        batch: pa.RecordBatch,
        out: OutputCollector,
    ) -> None:
        """Answer every question for each row of one input batch."""
        questions = questions_of(params.args.questions)
        states = states_of(batch.column(0), parse_json=params.args.parse_json)

        responses: list[api.Response | None]
        if any(s is not None for s in states):
            # Resolved only when there is something to ask, so an all-NULL
            # batch succeeds without a key.
            credentials = auth.for_call(params.secrets)
            with api.open_client() as client:
                responses = api.ask_many(
                    states,
                    questions,
                    credentials=credentials,
                    client=client,
                    model=params.args.model or api.DEFAULT_MODEL,
                    concurrency=params.args.concurrency,
                )
        else:
            responses = [None] * len(states)

        schema = output_schema_of(questions)
        columns: list[pa.Array] = [  # type: ignore[type-arg]
            pa.array(
                [_answer_cell(r.answers[name]) if r else None for r in responses],
                type=schema.field(name).type,
            )
            for name in questions
        ]
        columns.append(
            pa.array(
                [
                    {"model": r.model, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens} if r else None
                    for r in responses
                ],
                type=USAGE_TYPE,
            )
        )
        full = pa.RecordBatch.from_arrays(columns, schema=schema)
        out.emit(full.select(params.output_schema.names))
