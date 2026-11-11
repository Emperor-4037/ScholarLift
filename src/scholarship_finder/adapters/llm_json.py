"""The single lenient JSON parser for every LLM response in the app."""
from __future__ import annotations

import json
import re
from typing import Any

from scholarship_finder.errors import SchemaViolationError

_THINK_CLOSED = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"<think>.*$", re.DOTALL | re.IGNORECASE)
_FENCE_OPEN = re.compile(r"^```[a-zA-Z]*\s*")
_FENCE_CLOSE = re.compile(r"\s*```\s*$")


def strip_reasoning(text: str) -> str:
    """Remove closed and unclosed <think> blocks emitted by reasoning models."""
    text = _THINK_CLOSED.sub("", text or "")
    return _THINK_OPEN.sub("", text).strip()


def _slice_container(text: str) -> str:
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if not starts:
        return text
    start = min(starts)
    close = "}" if text[start] == "{" else "]"
    end = text.rfind(close)
    return text[start:end + 1] if end > start else text[start:]


def parse_json_lenient(text: str) -> Any:
    """Parse JSON from an LLM response: strip reasoning, fences, prose; repair if needed.

    Raises SchemaViolationError when nothing usable can be recovered.
    """
    cleaned = strip_reasoning(text)
    cleaned = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", cleaned.strip()))
    candidate = _slice_container(cleaned.strip())
    if not candidate:
        raise SchemaViolationError("llm_json", "Empty response", raw_output=text or "")
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    try:
        from json_repair import repair_json

        repaired = repair_json(candidate, return_objects=True)
    except Exception as exc:  # pragma: no cover - library failure
        raise SchemaViolationError("llm_json", f"JSON repair failed: {exc}", raw_output=text)
    if repaired in ("", None) or (isinstance(repaired, (dict, list)) and not repaired
                                   and candidate.strip() not in ("{}", "[]")):
        raise SchemaViolationError("llm_json", "Could not recover JSON", raw_output=text)
    return repaired


def as_list(value: Any, key: str | None = None) -> list:
    """Coerce a parsed JSON value to a list (unwrapping {key: [...]} when needed)."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if key and isinstance(value.get(key), list):
            return value[key]
        lists = [v for v in value.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
        return [value]
    return []
