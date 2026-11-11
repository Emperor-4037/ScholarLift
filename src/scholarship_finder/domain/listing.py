"""Listing pages → specific award pages, one hop, deterministically (no LLM).

A listing page (a directory/index of several awards) is not an opportunity,
but its links are the best possible leads: they sit on a trusted domain and
point straight at specific award pages.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin

from scholarship_finder.domain.source_filters import is_junk_url, is_news_or_report
from scholarship_finder.domain.url import canonicalize_url, extract_domain

_LINK_RE = re.compile(r"\[([^\]\n]{4,160})\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_AWARD_RE = re.compile(
    r"scholarship|fellowship|stipend|award|bursar|grant|stipendium|studentship|funding", re.I)
_SKIP_ANCHOR = re.compile(r"^(read more|more|learn more|click here|apply( now)?|here|details)$", re.I)
_SECOND_LEVEL = {"ac", "co", "gov", "edu", "org", "com", "govt", "go", "or", "ne", "net"}
_TOKEN = re.compile(r"[a-z]{3,}")


def registrable(domain: str) -> str:
    """'www2.daad.de' → 'daad.de'; 'ox.ac.uk' → 'ox.ac.uk'."""
    parts = [p for p in (domain or "").lower().split(".") if p]
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


@lru_cache(maxsize=1)
def official_domains() -> frozenset[str]:
    path = Path(__file__).parent / "data" / "official_sources.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return frozenset()
    return frozenset(d.lower() for v in data.values() if isinstance(v, list) for d in v)


def _domain_allowed(domain: str, base_reg: str, allowed: set[str] | frozenset[str]) -> bool:
    d = domain.lower()
    if registrable(d) == base_reg:
        return True
    return any(d == a or d.endswith("." + a) for a in allowed)


def _award_anchors(markdown: str, base_url: str) -> list[tuple[str, str]]:
    out, seen = [], set()
    for text, href in _LINK_RE.findall(markdown or ""):
        text = " ".join(text.split())
        if href.startswith(("#", "mailto:", "tel:", "javascript:")) or _SKIP_ANCHOR.match(text):
            continue
        if not _AWARD_RE.search(text):
            continue
        url = canonicalize_url(urljoin(base_url, href))
        if not url.startswith("http") or url == canonicalize_url(base_url) or url in seen:
            continue
        seen.add(url)
        out.append((text, url))
    return out


def is_listing(markdown: str, base_url: str = "") -> bool:
    """≥4 distinct links whose anchor text names an award."""
    return len(_award_anchors(markdown, base_url or "https://x.invalid/")) >= 4


def award_links(markdown: str, base_url: str, profile_tokens: set[str],
                allowed_domains: set[str] | frozenset[str] | None = None,
                limit: int = 5) -> list[tuple[str, str, float]]:
    """Top award links on a listing page as (anchor name, absolute url, score).

    Kept: same registrable domain as the listing page or an official-registry
    domain; never junk/news. Scored by overlap of anchor+URL tokens with the
    student's profile tokens (level, field, destination, nationality).
    """
    allowed = official_domains() if allowed_domains is None else allowed_domains
    base_reg = registrable(extract_domain(base_url))
    toks = {t.lower() for t in profile_tokens if t}
    scored = []
    for text, url in _award_anchors(markdown, base_url):
        if is_junk_url(url) or is_news_or_report(url, text):
            continue
        if not _domain_allowed(extract_domain(url), base_reg, allowed):
            continue
        words = set(_TOKEN.findall(f"{text} {url}".lower()))
        score = len(words & toks) + (0.5 if _AWARD_RE.search(url) else 0.0)
        scored.append((text, url, float(score)))
    scored.sort(key=lambda x: -x[2])
    return scored[:limit]
