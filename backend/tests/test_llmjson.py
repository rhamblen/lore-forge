"""Tests for the JSON repair layer.

Every malformation here is one a local instruct model actually produces. Run with:

    python -m pytest backend/tests -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llmjson import (  # noqa: E402
    JSONRepairError,
    coerce_json,
    coerce_list,
    coerce_object,
)


class TestCleanInput:
    def test_plain_object(self):
        assert coerce_json('{"a": 1}') == {"a": 1}

    def test_plain_array(self):
        assert coerce_json('[1, 2]') == [1, 2]

    def test_unicode_survives(self):
        assert coerce_json('{"name": "Sera Valdren — the clerk"}')["name"].endswith("clerk")


class TestFences:
    def test_json_fence(self):
        assert coerce_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_bare_fence(self):
        assert coerce_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_uppercase_fence(self):
        assert coerce_json('```JSON\n{"a": 1}\n```') == {"a": 1}


class TestChatter:
    def test_preamble(self):
        assert coerce_json('Sure! Here is the extraction:\n{"a": 1}') == {"a": 1}

    def test_postamble(self):
        assert coerce_json('{"a": 1}\n\nLet me know if you need more!') == {"a": 1}

    def test_both(self):
        text = 'Certainly.\n```json\n{"a": 1}\n```\nHope that helps.'
        assert coerce_json(text) == {"a": 1}


class TestRepairs:
    def test_trailing_comma_object(self):
        assert coerce_json('{"a": 1,}') == {"a": 1}

    def test_trailing_comma_array(self):
        assert coerce_json('{"a": [1, 2,]}') == {"a": [1, 2]}

    def test_trailing_comma_nested(self):
        assert coerce_json('{"a": {"b": 1,},}') == {"a": {"b": 1}}

    def test_smart_quotes_as_delimiters(self):
        # The model echoed typographic quotes around a key/value.
        assert coerce_json('{“a”: “x”}') == {"a": "x"}


class TestStringsAreRespected:
    """The repairs must not corrupt content. These are the regression guards."""

    def test_brace_inside_string(self):
        # A naive find('{')..rfind('}') slice mangles this.
        assert coerce_json('{"t": "a } brace"}') == {"t": "a } brace"}

    def test_bracket_inside_string(self):
        assert coerce_json('{"t": "list [1] here"}') == {"t": "list [1] here"}

    def test_escaped_quote_inside_string(self):
        assert coerce_json(r'{"t": "she said \"no\""}') == {"t": 'she said "no"'}

    def test_apostrophe_inside_string_is_not_rewritten(self):
        # A curly apostrophe INSIDE a string is the book's own punctuation and must
        # survive — de-smartening applies outside strings only.
        out = coerce_json('{"t": "the Court’s ledger"}')
        assert out["t"] == "the Court’s ledger"

    def test_comma_brace_sequence_inside_string_survives(self):
        assert coerce_json('{"t": "a ,} b"}') == {"t": "a ,} b"}

    def test_real_trailing_comma_does_not_corrupt_a_string_holding_comma_brace(self):
        """The regression that matters.

        Both conditions at once: a genuine trailing comma (so the repair path runs) AND
        a value containing ',}'. A regex-based repair eats the comma inside the value
        and silently rewrites the extracted text.
        """
        out = coerce_json('{"t": "a ,} b",}')
        assert out == {"t": "a ,} b"}

    def test_trailing_comma_repair_with_comma_bracket_in_string(self):
        out = coerce_json('{"t": "x ,] y", "n": [1,],}')
        assert out == {"t": "x ,] y", "n": [1]}

    def test_whitespace_and_newline_before_close(self):
        assert coerce_json('{"a": 1,\n  \n}') == {"a": 1}

    def test_escaped_backslash_before_quote_ends_string(self):
        # "path\\" then a real trailing comma — the escape handling must not run on.
        out = coerce_json(r'{"p": "C:\\", "q": 2,}')
        assert out == {"p": "C:\\", "q": 2}


class TestFailures:
    def test_truncated_is_reported_not_guessed(self):
        with pytest.raises(JSONRepairError, match="truncated"):
            coerce_json('{"a": 1, "b": {"c":')

    def test_empty(self):
        with pytest.raises(JSONRepairError, match="nothing"):
            coerce_json("   ")

    def test_prose_only(self):
        with pytest.raises(JSONRepairError, match="no JSON value"):
            coerce_json("I could not find any progression rules in this passage.")


class TestObjectAndList:
    def test_object_from_object(self):
        assert coerce_object('{"rules": []}') == {"rules": []}

    def test_list_under_key(self):
        assert coerce_list('{"rules": [{"a": 1}]}', "rules") == [{"a": 1}]

    def test_bare_list_accepted(self):
        assert coerce_list('[{"a": 1}]', "rules") == [{"a": 1}]

    def test_single_object_promoted_to_list(self):
        # "Return a list" + one result = model often drops the list.
        assert coerce_list('{"kind": "xp", "name": "x"}', "rules") == [
            {"kind": "xp", "name": "x"}
        ]

    def test_empty_result_is_empty_list_not_error(self):
        # Most chunks contain no rules; that must be cheap and quiet.
        assert coerce_list('{"rules": []}', "rules") == []

    def test_object_first_when_array_appears_later(self):
        assert coerce_json('{"a": [1]} trailing [2]') == {"a": [1]}
