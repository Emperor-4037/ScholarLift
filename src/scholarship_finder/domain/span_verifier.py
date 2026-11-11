"""Deterministic evidence-span verification.

Stage 1 of the two-phase anti-hallucination pipeline (architecture §2.5).

Normalization pipeline applied to BOTH the page text and the extracted span:
  1. HTML entity unescaping (html.unescape)
  2. Unicode NFKC normalization
  3. Case-folding (casefold, not just lower)
  4. Markdown link stripping: [text](url) → text
  5. Quote unification (smart quotes → ASCII)
  6. Dash unification (em/en dashes → ASCII hyphen)
  7. Markdown formatting symbols stripped ([*_`~#>|]+)
  8. Zero-width / invisible chars stripped
  9. Whitespace collapse (any run of whitespace → single space, stripped)

The normalized span must be a substring of the normalized page text,
with length between 15 and 400 characters.
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass


# ── Quote & dash unification tables ──────────────────────────────────────────

_QUOTE_TABLE: dict[str, str] = {
    "\u2018": "'", "\u2019": "'",   # curly single quotes
    "\u201c": '"', "\u201d": '"',   # curly double quotes
    "\u201a": "'", "\u201b": "'",   # low/high single quotes
    "\u201e": '"', "\u201f": '"',   # low/high double quotes
    "\u00ab": '"', "\u00bb": '"',   # guillemets
    "\u2039": "'", "\u203a": "'",   # single guillemets
    "\u0060": "'",                  # backtick
    "\u00b4": "'",                  # acute accent used as quote
}

_DASH_PATTERN: re.Pattern[str] = re.compile(
    r"[\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d]"
)

_WHITESPACE_PATTERN: re.Pattern[str] = re.compile(r"\s+")

# Markdown link: [link text](url) → keep only link text
_MD_LINK_PATTERN: re.Pattern[str] = re.compile(r"\[([^\]]+)\]\([^)]+\)")

# Markdown formatting symbols to strip (stand-alone, not inside words)
_MD_FORMAT_PATTERN: re.Pattern[str] = re.compile(r"[*_`~#>|]+")

# Zero-width and invisible Unicode characters
_ZW_PATTERN: re.Pattern[str] = re.compile(
    r"[\u200b\u200c\u200d\u00ad\ufeff]"
)


# ── Core normalizer ───────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    """Apply the canonical normalization pipeline to *text*.

    Idempotent: normalize_text(normalize_text(t)) == normalize_text(t).
    """
    # 1. HTML entity unescaping — catches &amp;, &nbsp;, etc.
    text = html.unescape(text)
    # 2. NFKC — compatibility decomposition followed by canonical composition
    text = unicodedata.normalize("NFKC", text)
    # 3. Case-fold
    text = text.casefold()
    # 4. Markdown link stripping: [text](url) → text
    text = _MD_LINK_PATTERN.sub(r"\1", text)
    # 5. Quote unification
    for char, replacement in _QUOTE_TABLE.items():
        text = text.replace(char, replacement)
    # 6. Dash unification
    text = _DASH_PATTERN.sub("-", text)
    # 7. Strip Markdown formatting symbols
    text = _MD_FORMAT_PATTERN.sub(" ", text)
    # 8. Strip zero-width / invisible chars
    text = _ZW_PATTERN.sub("", text)
    # 9. Whitespace collapse
    text = _WHITESPACE_PATTERN.sub(" ", text).strip()
    return text


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class SpanVerificationResult:
    matched: bool
    offset: int | None = None                # character offset in norm_page_text
    context_window: str | None = None        # ±150 chars around the match
    failure_reason: str | None = None


# ── Public API ────────────────────────────────────────────────────────────────

def extract_context_window(text: str, offset: int, span_len: int, window: int = 150) -> str:
    """Return up to *window* characters on either side of the match in *text*."""
    start = max(0, offset - window)
    end = min(len(text), offset + span_len + window)
    return text[start:end]


def verify_span(
    span: str,
    page_text: str,
    min_len: int = 15,
    max_len: int = 400,
    fuzzy: bool = False,
) -> SpanVerificationResult:
    """Verify that *span* is a normalized substring of *page_text*.

    Args:
        span:      The verbatim text extracted by the LLM.
        page_text: The raw page text fetched by the Auditor.
        min_len:   Minimum length after normalization (default 15).
        max_len:   Maximum length after normalization (default 400).
        fuzzy:     If True, attempt fuzzy matching when exact match fails (default False).

    Returns:
        SpanVerificationResult with matched status, offset, context window,
        and a failure reason when not matched.
    """
    norm_span = normalize_text(span)
    norm_page = normalize_text(page_text)

    if len(norm_span) < min_len:
        return SpanVerificationResult(
            matched=False,
            failure_reason=f"Span too short after normalization: {len(norm_span)} < {min_len}",
        )
    if len(norm_span) > max_len:
        return SpanVerificationResult(
            matched=False,
            failure_reason=f"Span too long after normalization: {len(norm_span)} > {max_len}",
        )

    offset = norm_page.find(norm_span)
    if offset != -1:
        context = extract_context_window(norm_page, offset, len(norm_span))
        return SpanVerificationResult(
            matched=True,
            offset=offset,
            context_window=context,
        )

    # Fuzzy matching attempt if enabled
    if fuzzy:
        # Try to find a substring that matches at least 80% of the span
        min_match_ratio = 0.8
        best_match_ratio = 0.0
        best_offset = -1
        
        # Search for partial matches using sliding window
        span_len = len(norm_span)
        for i in range(len(norm_page) - span_len + 1):
            window = norm_page[i:i + span_len]
            matches = sum(1 for a, b in zip(window, norm_span) if a == b)
            ratio = matches / span_len
            if ratio > best_match_ratio:
                best_match_ratio = ratio
                best_offset = i
            if ratio == 1.0:  # Perfect match found
                break
        
        if best_match_ratio >= min_match_ratio:
            context = extract_context_window(norm_page, best_offset, span_len)
            return SpanVerificationResult(
                matched=True,
                offset=best_offset,
                context_window=context,
            )
        
        return SpanVerificationResult(
            matched=False,
            failure_reason=f"Fuzzy match failed: best match ratio {best_match_ratio:.2f} < {min_match_ratio}",
        )

    return SpanVerificationResult(
        matched=False,
        failure_reason="Span not found in normalized page text",
    )
