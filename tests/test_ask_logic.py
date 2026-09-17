# Copyright 2026 Query Farm LLC - https://query.farm

"""``ask()``'s pure logic: question validation, state conversion, output shape.

No worker and no HTTP here — ``test_ask_function.py`` drives the real thing.
These pin the rules a user meets as error messages, and the conversions where a
silent mistake would send the API something other than what the row held.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pytest

from vgi_typesafe.ask import (
    ANSWER_TYPES,
    USAGE_TYPE,
    check_state_type,
    output_schema_of,
    questions_of,
    states_of,
    to_json,
)

CHOICE = {"type": "choice", "instructions": "Which?", "criteria": {"a": "x", "b": "y"}}
NOUL = {"type": "noul", "instructions": "Is it?"}
SCORE = {"type": "score", "instructions": "How much?", "criteria": ["low", "high"]}


class TestQuestionsOf:
    def test_a_struct_literal(self) -> None:
        assert questions_of({"c": CHOICE, "n": NOUL, "s": SCORE}) == {"c": CHOICE, "n": NOUL, "s": SCORE}

    def test_order_is_preserved_because_it_is_the_column_order(self) -> None:
        assert list(questions_of({"z": NOUL, "a": NOUL, "m": NOUL})) == ["z", "a", "m"]

    def test_a_json_string(self) -> None:
        text = '{"n": {"type": "noul", "instructions": "Is it?"}}'
        assert questions_of(text) == {"n": NOUL}

    def test_a_map_arrives_as_pairs_padded_with_nulls(self) -> None:
        """A MAP's values share one struct type, so DuckDB pads the absent fields with NULL."""
        as_map = [("n", {"type": "noul", "instructions": "Is it?", "criteria": None})]
        assert questions_of(as_map) == {"n": NOUL}

    def test_choice_criteria_given_as_a_map(self) -> None:
        question = {**CHOICE, "criteria": [("a", "x"), ("b", "y")]}
        assert questions_of({"c": question}) == {"c": CHOICE}

    def test_an_any_arrow_wrapper_and_an_arrow_scalar_are_unwrapped(self) -> None:
        class Wrapped:
            value = pa.scalar({"n": NOUL})

        assert questions_of(Wrapped()) == {"n": NOUL}

    def test_type_is_case_insensitive(self) -> None:
        assert questions_of({"n": {**NOUL, "type": "NOUL"}})["n"]["type"] == "noul"

    def test_structured_criteria_pass_through(self) -> None:
        criteria = {"a": {"what": "A things", "not_for": "B things", "examples": ["a1"]}, "b": "plain"}
        assert questions_of({"c": {**CHOICE, "criteria": criteria}})["c"]["criteria"] == criteria

    def test_noul_criteria_are_optional_and_partial(self) -> None:
        assert "criteria" not in questions_of({"n": NOUL})["n"]
        assert questions_of({"n": {**NOUL, "criteria": {"true": "yes"}}})["n"]["criteria"] == {"true": "yes"}

    @pytest.mark.parametrize(
        ("raw", "fragment"),
        [
            (None, "requires 'questions'"),
            ({}, "requires 'questions'"),
            ("not json", "not valid JSON"),
            ("[1, 2]", "requires 'questions'"),
            ({"q": "just a string"}, "question 'q' must be a struct"),
            ({"q": {**NOUL, "weight": 2}}, "question 'q' has unknown field\\(s\\) weight"),
            ({"q": {"instructions": "x"}}, "question 'q' has type None"),
            ({"q": {**NOUL, "type": "essay"}}, "expected one of choice, noul, score"),
            ({"q": {"type": "noul"}}, "question 'q' requires 'instructions'"),
            ({"q": {"type": "noul", "instructions": "  "}}, "question 'q' requires 'instructions'"),
            ({"q": {"type": "choice", "instructions": "x"}}, "choice question 'q' requires 'criteria'"),
            ({"q": {**CHOICE, "criteria": ["a", "b"]}}, "choice question 'q' requires 'criteria'"),
            ({"q": {**CHOICE, "criteria": {" ": "x"}}}, "blank option name"),
            ({"q": {**CHOICE, "criteria": {f"o{i}": "" for i in range(256)}}}, "more than 255 options"),
            ({"q": {"type": "score", "instructions": "x"}}, "score question 'q' requires 'criteria'"),
            ({"q": {**SCORE, "criteria": ["only one"]}}, "2-10 level descriptions"),
            ({"q": {**SCORE, "criteria": [str(i) for i in range(11)]}}, "2-10 level descriptions"),
            ({"q": {**SCORE, "criteria": {"low": "x", "high": "y"}}}, "an ordered list"),
            ({"q": {**NOUL, "criteria": {"maybe": "x"}}}, "only 'true' and 'false'"),
            ({" ": NOUL}, "blank name"),
            ({"usage": NOUL}, "may not be named 'usage'"),
            ({"Usage": NOUL}, "may not be named 'usage'"),
            ({"dept": NOUL, "DEPT": NOUL}, "'DEPT' is repeated"),
        ],
    )
    def test_invalid_questions_name_the_problem(self, raw: Any, fragment: str) -> None:
        with pytest.raises(ValueError, match=fragment):
            questions_of(raw)


class TestOutputSchema:
    def test_one_struct_per_question_then_usage(self) -> None:
        schema = output_schema_of(questions_of({"c": CHOICE, "n": NOUL, "s": SCORE}))
        assert schema.names == ["c", "n", "s", "usage"]
        assert schema.types == [
            ANSWER_TYPES["choice"],
            ANSWER_TYPES["noul"],
            ANSWER_TYPES["score"],
            USAGE_TYPE,
        ]

    def test_the_column_comment_says_what_was_asked(self) -> None:
        schema = output_schema_of(questions_of({"n": NOUL}))
        assert schema.field("n").metadata == {b"comment": b"noul: Is it?"}

    def test_score_probabilities_are_keyed_by_integer_level(self) -> None:
        assert ANSWER_TYPES["score"].field("probabilities").type == pa.map_(pa.int32(), pa.float64())
        assert ANSWER_TYPES["choice"].field("probabilities").type == pa.map_(pa.string(), pa.float64())


class TestStateType:
    @pytest.mark.parametrize(
        "kind",
        [
            pa.string(),
            pa.large_string(),
            pa.struct([("a", pa.int64())]),
            pa.list_(pa.string()),
            pa.map_(pa.string(), pa.string()),
        ],
    )
    def test_accepted(self, kind: pa.DataType) -> None:
        check_state_type(kind, parse_json=False)

    @pytest.mark.parametrize("kind", [pa.int64(), pa.float64(), pa.bool_(), pa.date32(), pa.binary()])
    def test_a_bare_scalar_is_rejected_with_a_way_out(self, kind: pa.DataType) -> None:
        with pytest.raises(ValueError, match=r"must be VARCHAR, STRUCT, LIST or MAP.*wrap it in a struct"):
            check_state_type(kind, parse_json=False)

    def test_parse_json_needs_text(self) -> None:
        check_state_type(pa.string(), parse_json=True)
        with pytest.raises(ValueError, match="parse_json => true needs a VARCHAR or JSON state"):
            check_state_type(pa.struct([("a", pa.int64())]), parse_json=True)


class TestToJson:
    def test_a_map_becomes_an_object_and_a_list_of_pairs_stays_a_list(self) -> None:
        """Identical once in Python — which is why conversion is driven by the Arrow type."""
        pairs = [("a", "x"), ("b", "y")]
        assert to_json(pairs, pa.map_(pa.string(), pa.string())) == {"a": "x", "b": "y"}
        as_list = pa.list_(pa.struct([("k", pa.string()), ("v", pa.string())]))
        assert to_json([{"k": "a", "v": "x"}], as_list) == [{"k": "a", "v": "x"}]

    def test_an_empty_map_is_an_object_not_an_array(self) -> None:
        assert to_json([], pa.map_(pa.string(), pa.string())) == {}
        assert to_json([], pa.list_(pa.string())) == []

    def test_nested(self) -> None:
        kind = pa.struct(
            [
                ("tags", pa.list_(pa.string())),
                ("attrs", pa.map_(pa.string(), pa.int64())),
                ("missing", pa.string()),
            ]
        )
        value = {"tags": ["a", "b"], "attrs": [("n", 1)], "missing": None}
        assert to_json(value, kind) == {"tags": ["a", "b"], "attrs": {"n": 1}, "missing": None}

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (Decimal("1.50"), 1.5),
            (Decimal("3"), 3),
            (dt.date(2026, 1, 2), "2026-01-02"),
            (dt.datetime(2026, 1, 2, 3, 4, 5), "2026-01-02T03:04:05"),
            (float("nan"), None),
            (float("inf"), None),
            (True, True),
            (7, 7),
        ],
    )
    def test_scalars_become_json_values(self, value: Any, expected: Any) -> None:
        assert to_json({"v": value}, pa.struct([("v", pa.null())])) == {"v": expected}

    def test_a_blob_is_refused_rather_than_mangled(self) -> None:
        with pytest.raises(ValueError, match="BLOB"):
            to_json({"v": b"\x00"}, pa.struct([("v", pa.binary())]))


class TestStatesOf:
    def test_text_is_sent_verbatim(self) -> None:
        column = pa.array(['{"a": 1}', None, "plain"])
        assert states_of(column, parse_json=False) == ['{"a": 1}', None, "plain"]

    def test_parse_json_sends_it_structured(self) -> None:
        column = pa.array(['{"a": 1}', None, '["x"]', '"just text"'])
        assert states_of(column, parse_json=True) == [{"a": 1}, None, ["x"], "just text"]

    @pytest.mark.parametrize(
        ("text", "fragment"), [("{oops", "not valid JSON"), ("42", "must be an object, array")]
    )
    def test_parse_json_failures_raise(self, text: str, fragment: str) -> None:
        with pytest.raises(ValueError, match=fragment):
            states_of(pa.array([text]), parse_json=True)

    def test_a_struct_with_no_content_is_not_asked_about(self) -> None:
        """Not NULL in SQL, but nothing to judge — asking would bill a meaningless answer."""
        kind = pa.struct([("message", pa.string()), ("tags", pa.list_(pa.string()))])
        column = pa.array(
            [
                {"message": "hi", "tags": []},
                {"message": None, "tags": None},
                {"message": None, "tags": []},
                {"message": None, "tags": [None]},
                None,
            ],
            type=kind,
        )
        states = states_of(column, parse_json=False)
        assert states[0] == {"message": "hi", "tags": []}
        assert states[1:] == [None, None, None, None]

    def test_falsy_values_are_still_content(self) -> None:
        kind = pa.struct([("n", pa.int64()), ("flag", pa.bool_()), ("s", pa.string())])
        column = pa.array(
            [{"n": 0, "flag": None, "s": None}, {"n": None, "flag": False, "s": None}], type=kind
        )
        assert states_of(column, parse_json=False) == [
            {"n": 0, "flag": None, "s": None},
            {"n": None, "flag": False, "s": None},
        ]
