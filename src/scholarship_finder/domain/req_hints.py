"""Deterministic regex hint pre-pass — feeds the LLM extraction prompt and
fills fields the LLM omits (each hint still needs span verification)."""
from __future__ import annotations
import re
from typing import Any

_MONTHS = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
_RE = {
    "ielts": re.compile(r"IELTS[^.\n]{0,50}?\b([4-9](?:\.[05])?)\b(?!\s*(?:%|years?|months?))", re.I),
    "toefl": re.compile(r"TOEFL[^.\n]{0,50}?\b(\d{2,3})\b", re.I),
    "pte": re.compile(r"PTE[^.\n]{0,50}?\b(\d{2})\b", re.I),
    "duolingo": re.compile(r"Duolingo[^.\n]{0,50}?\b(\d{2,3})\b", re.I),
    "gpa": re.compile(r"(?:GPA|CGPA|grade point average)[^.\n]{0,50}?\b(\d{1,2}(?:\.\d{1,2})?)\s*(?:/|out of|on a scale of|of)\s*(4\.0|4\.3|4|5\.0|5|10\.0|10|20|100)\b", re.I),
    "gpa_pct": re.compile(r"(?:(?:GPA|marks?|average|aggregate|grade)[^.\n]{0,40}?\b(\d{2}(?:\.\d)?)\s*%|\b(\d{2}(?:\.\d)?)\s*%\s*(?:marks?|average|aggregate|overall))", re.I),
    "deadline": re.compile(
        r"(?:deadline|closing date|closes?|apply by|due|applications? (?:must be|are) (?:received|submitted) by)"
        r"[^.\n]{0,40}?\b((?:\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTHS + r",?\s+\d{4})|(?:" + _MONTHS
        + r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}))", re.I),
    "amount": re.compile(r"(INR|USD|EUR|GBP|AUD|CAD|SGD|NZD|CHF|€|£|\$|S\$|A\$|C\$)\s?(\d[\d,]{2,}(?:\.\d+)?)", re.I),
}
_TEST_RANGE = {"ielts": (4.0, 9.0), "toefl": (40, 120), "pte": (30, 90), "duolingo": (60, 160)}
_DEGREE = re.compile(r"\b(bachelors?|masters?|doctoral|doctorate|phd|postdoc|undergraduate|postgraduate|diploma)\b", re.I)


def _sentence(text: str, start: int, end: int) -> str:
    a = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    ends = [i for i in (text.find(".", end), text.find("\n", end)) if i >= 0]
    b = min(ends) if ends else len(text)
    return text[a:b].strip()[:400]


def extract_hints(text: str) -> dict[str, Any]:
    """{key: {"value": matched text, "groups": tuple, "sentence": source sentence}}."""
    if not text:
        return {}
    out: dict[str, Any] = {}
    for key, rx in _RE.items():
        for m in rx.finditer(text):
            if key in _TEST_RANGE:
                lo, hi = _TEST_RANGE[key]
                try:
                    if not lo <= float(m.group(1)) <= hi:
                        continue
                except ValueError:
                    continue
            out[key] = {"value": m.group(0).strip(), "groups": m.groups(),
                        "sentence": _sentence(text, m.start(), m.end())}
            break
    dm = _DEGREE.findall(text)
    if dm:
        out["degree_levels"] = {"value": sorted({d.lower() for d in dm})}
    return out


def hints_block(hints: dict[str, Any]) -> str:
    """Compact HINTS: block for the extraction prompt."""
    if not hints:
        return ""
    return "HINTS (regex matches on this page; verify before use): " + "; ".join(
        f"{k}={v['value']}" for k, v in hints.items() if k != "degree_levels")
