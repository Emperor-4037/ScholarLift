"""Deterministic pre-gates applied before any LLM extraction call."""
from __future__ import annotations
import re
from typing import Any

from scholarship_finder.domain.source_filters import is_junk_url, is_news_or_report

_FUNDING_RE = re.compile(
    r"scholarship|fellowship|award|stipend|bursary|grant|funded|funding|"
    r"tuition|waiver|financial (aid|support)|cover(ed|s)? (the )?cost", re.I)
_LEVEL_DOCTORAL = re.compile(r"\b(doctoral|doctorate|phd|d\.phil|postdoc|post-doctoral)\b", re.I)
_LEVEL_MASTERS = re.compile(r"\b(masters?|msc|ma\b|mba|graduate|postgraduate|pg)\b", re.I)
_LEVEL_BACHELOR = re.compile(r"\b(bachelors?|bsc|ba\b|undergraduate|ug)\b", re.I)
_LEVEL_ANY = re.compile(r"all (degree )?levels|any degree|all programs", re.I)


def pregate(cand: Any, text: str, profile: Any = None) -> tuple[bool, str]:
    """(keep, reason). Reasons: junk, too_short, no_funding_terms,
    level_mismatch, news_or_report."""
    url = getattr(cand, "canonical_url", "") or getattr(cand, "url", "") or ""
    title = getattr(cand, "title", "") or ""
    if is_junk_url(url) or is_junk_url(title):
        return False, "junk"
    if is_news_or_report(url, title):
        return False, "news_or_report"
    if len(text or "") < 400:
        return False, "too_short"
    if not _FUNDING_RE.search(text):
        return False, "no_funding_terms"
    # Level compatibility: drop pages that are exclusively the wrong level.
    from scholarship_finder.domain.eligibility import degree_levels_of
    target_txt = (getattr(getattr(profile, "preferences", None),
                          "degree_level", "") or "") if profile else ""
    levels = degree_levels_of(target_txt)
    hay = f"{title} {url} {text}"  # title/URL often carry the level signal
    if "phd" in levels:
        pass  # PhD targets are never level-dropped
    elif "masters" in levels:
        wrong = ((_LEVEL_DOCTORAL.search(hay) or _LEVEL_BACHELOR.search(hay))
                 and not _LEVEL_MASTERS.search(hay)
                 and not _LEVEL_ANY.search(hay))
        if wrong:
            return False, "level_mismatch"
    elif "bachelors" in levels:
        if ((_LEVEL_MASTERS.search(hay) or _LEVEL_DOCTORAL.search(hay))
                and not _LEVEL_BACHELOR.search(hay)):
            return False, "level_mismatch"
    return True, ""
