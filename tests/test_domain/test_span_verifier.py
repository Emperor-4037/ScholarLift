"""Tests for the evidence-span verifier."""
from __future__ import annotations

import pytest

from scholarship_finder.domain.span_verifier import normalize_text, verify_span


class TestNormalizeText:
    def test_casefold(self):
        assert normalize_text("GPA") == "gpa"

    def test_nfkc(self):
        # Full-width characters → ASCII
        assert normalize_text("\uff47\uff50\uff41") == "gpa"

    def test_smart_quotes(self):
        assert normalize_text("\u201cHello\u201d") == '"hello"'

    def test_em_dash(self):
        assert normalize_text("well\u2014being") == "well-being"

    def test_whitespace_collapse(self):
        assert normalize_text("  hello   world  ") == "hello world"

    def test_idempotent(self):
        text = "  GPA: 3.7 — \u201cExcellent\u201d  "
        once = normalize_text(text)
        twice = normalize_text(once)
        assert once == twice


class TestVerifySpan:
    PAGE = (
        "The minimum GPA requirement is 3.0 on a 4.0 scale. "
        "Students with a GPA below 3.0 are not eligible. "
        "Funding covers full tuition and living expenses up to $25,000 per year."
    )

    def test_matching_span(self):
        span = "minimum GPA requirement is 3.0 on a 4.0 scale"
        result = verify_span(span, self.PAGE)
        assert result.matched is True
        assert result.offset is not None
        assert result.context_window is not None

    def test_case_insensitive_match(self):
        span = "MINIMUM GPA REQUIREMENT IS 3.0 ON A 4.0 SCALE"
        result = verify_span(span, self.PAGE)
        assert result.matched is True

    def test_span_too_short(self):
        result = verify_span("GPA", self.PAGE, min_len=15)
        assert result.matched is False
        assert "too short" in result.failure_reason.lower()

    def test_span_too_long(self):
        long_span = "A" * 401
        result = verify_span(long_span, self.PAGE, max_len=400)
        assert result.matched is False
        assert "too long" in result.failure_reason.lower()

    def test_span_not_in_page(self):
        result = verify_span("this text does not appear in the page at all", self.PAGE)
        assert result.matched is False
        assert "not found" in result.failure_reason.lower()

    def test_context_window_contains_match(self):
        span = "Funding covers full tuition and living expenses"
        result = verify_span(span, self.PAGE)
        assert result.matched is True
        assert "funding" in result.context_window.lower()

    def test_empty_span_rejected(self):
        result = verify_span("", self.PAGE)
        assert result.matched is False
