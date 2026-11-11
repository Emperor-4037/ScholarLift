"""Tests for the lenient LLM JSON parser (adapters/llm_json.py)."""
from __future__ import annotations

import pytest

from scholarship_finder.adapters.llm_json import parse_json_lenient, strip_reasoning
from scholarship_finder.errors import SchemaViolationError


def test_plain_object():
    assert parse_json_lenient('{"a": 1}') == {"a": 1}


def test_plain_array():
    assert parse_json_lenient('[1, 2, 3]') == [1, 2, 3]


def test_fenced_json():
    text = '```json\n{"a": 1}\n```'
    assert parse_json_lenient(text) == {"a": 1}


def test_fenced_no_lang():
    text = '```\n[1, 2]\n```'
    assert parse_json_lenient(text) == [1, 2]


def test_closed_think_block_stripped():
    text = '<think>reasoning here</think>\n{"a": 1}'
    assert parse_json_lenient(text) == {"a": 1}


def test_unclosed_think_block_stripped():
    text = 'prefix {"a": 1}\n<think>still thinking forever'
    assert parse_json_lenient(text) == {"a": 1}


def test_prose_around_json():
    text = 'Here is the answer:\n{"a": 1, "b": [2]}\nHope that helps!'
    assert parse_json_lenient(text) == {"a": 1, "b": [2]}


def test_truncated_json_repaired():
    parsed = parse_json_lenient('{"a": 1, "b": "unterminated')
    assert parsed["a"] == 1


def test_garbage_raises_schema_violation():
    with pytest.raises(SchemaViolationError):
        parse_json_lenient("no json here at all")


def test_empty_raises_schema_violation():
    with pytest.raises(SchemaViolationError):
        parse_json_lenient("")


def test_strip_reasoning_leaves_text():
    assert strip_reasoning("<think>x</think>  hi  ") == "hi"
