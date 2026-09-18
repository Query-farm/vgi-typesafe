# Copyright 2026 Query Farm LLC - https://query.farm

"""``ask_dynamic()``'s pure logic: per-row question validation and the fixed result shape.

``ask()`` validates once, at bind, so a bad question there fails a query before
it runs. Here the questions are data, and the failure happens mid-scan — which is
why every rule these pin is about a message being able to point at the *one* row
that was wrong. ``test_ask_dynamic_function.py`` drives the real worker.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from vgi_typesafe.ask_dynamic import ANSWERS_SCHEMA, EXCERPT_LIMIT, check_questions_type, questions_for_row

CHOICE = {"type": "choice", "instructions": "Which?", "criteria": {"a": "x", "b": "y"}}
NOUL = {"type": "noul", "instructions": "Is it?"}


class TestQuestionsForRow:
    """One row's questions, and the messages a user meets when a row is wrong."""

    def test_json_text_is_the_form_that_survives_differing_shapes(self) -> None:
        """A JSON string is reconciled against no other row, so it is the form the docs recommend."""
        assert questions_for_row(0, '{"n": {"type": "noul", "instructions": "Is it?"}}', "x") == {"n": NOUL}

    def test_a_struct_value_is_accepted_too(self) -> None:
        """A struct literal is what a SQL user writes when every row's questions look alike."""
        assert questions_for_row(0, {"c": CHOICE}, "x") == {"c": CHOICE}

    def test_struct_padding_from_column_unification_is_dropped(self) -> None:
        """DuckDB pads a heterogeneous questions column with NULLs; a padded row must still ask only its own.

        This is the whole reason a STRUCT column can carry different questions on
        different rows at all. If the padding reached the API, every row would
        ask a malformed version of every other row's question.
        """
        padded = {"c": CHOICE, "n": None}
        assert questions_for_row(0, padded, "x") == {"c": CHOICE}

    def test_a_map_arrives_as_pairs(self) -> None:
        """A MAP-typed column delivers (key, value) pairs rather than a dict."""
        assert questions_for_row(0, [("n", NOUL)], "x") == {"n": NOUL}

    def test_usage_is_a_usable_question_name_here(self) -> None:
        """`ask()` reserves it for its trailing column; this function has no such column to collide with."""
        assert questions_for_row(0, {"usage": NOUL}, "x") == {"usage": NOUL}

    def test_a_missing_value_points_at_the_row_by_index_and_by_content(self) -> None:
        """A NULL in a questions column is a data problem, so the message must let the user find that row.

        Under a correlated LATERAL, DuckDB hands over one row per batch and the
        index is always 0 — so the index alone would locate nothing. The row's
        own content is what identifies it in the caller's table.
        """
        with pytest.raises(ValueError, match=r"ask_dynamic\(\): row 7 of this input batch carries no questions"):
            questions_for_row(7, None, "a lost package")
        with pytest.raises(ValueError, match="a lost package"):
            questions_for_row(7, None, "a lost package")

    @pytest.mark.parametrize(
        ("raw", "fragment"),
        [
            ({"q": {"type": "essay", "instructions": "x"}}, "expected one of choice, noul, score"),
            ({"q": {"type": "score", "instructions": "x", "criteria": ["one"]}}, "2-10 level descriptions"),
            ("not json", "not valid JSON"),
            ({}, "requires 'questions'"),
        ],
    )
    def test_a_bad_question_keeps_the_shared_message_and_gains_the_row(self, raw: Any, fragment: str) -> None:
        """The rules are `ask()`'s, so the wording should be too — plus the row, which `ask()` never needs.

        The prefix matters as much as the rule: a user who called this function
        must not be told that `ask()` rejected something.
        """
        with pytest.raises(ValueError, match=fragment) as excinfo:
            questions_for_row(3, raw, "x")
        assert str(excinfo.value).startswith("ask_dynamic()")
        assert "row 3 of this input batch" in str(excinfo.value)

    def test_the_offending_value_is_quoted_back_but_bounded(self) -> None:
        """Without the value the user greps their own input; with an unbounded one the error is unreadable."""
        huge = {f"q{i}": {"type": "essay", "instructions": "x"} for i in range(200)}
        with pytest.raises(ValueError) as excinfo:
            questions_for_row(0, huge, "x")
        quoted = str(excinfo.value).split("whose questions were ", 1)[1]
        assert quoted.endswith("...")
        assert len(quoted) == EXCERPT_LIMIT + 3


class TestQuestionsColumnType:
    """Which column types can carry a question at all, decided at bind."""

    @pytest.mark.parametrize(
        "kind",
        [pa.string(), pa.large_string(), pa.struct([("n", pa.string())]), pa.map_(pa.string(), pa.string())],
    )
    def test_accepted(self, kind: pa.DataType) -> None:
        """Every shape a question can be written in from SQL."""
        check_questions_type(kind)

    @pytest.mark.parametrize("kind", [pa.int64(), pa.float64(), pa.bool_(), pa.list_(pa.string()), pa.null()])
    def test_rejected_at_bind_with_a_way_out(self, kind: pa.DataType) -> None:
        """A column of the wrong type is wrong for every row, so failing per row would be noise."""
        with pytest.raises(ValueError, match=r"questions must be VARCHAR, STRUCT or MAP.*JSON text"):
            check_questions_type(kind)


class TestResultShape:
    """The four columns, which are fixed precisely because the answers are not."""

    def test_the_answers_are_one_column_and_the_cost_is_not(self) -> None:
        """Burying `model` and the token counts in the JSON would make a query cost a parse to read."""
        assert ANSWERS_SCHEMA.names == ["answers", "model", "input_tokens", "output_tokens"]
        assert ANSWERS_SCHEMA.types == [pa.string(), pa.string(), pa.int64(), pa.int64()]

    def test_every_column_carries_a_comment(self) -> None:
        """DESCRIBE and the published result schema are both built from these."""
        assert all(f.metadata and b"comment" in f.metadata for f in ANSWERS_SCHEMA)
