"""Tests for the tri-state eligibility rule evaluator."""
from __future__ import annotations

import pytest

from scholarship_finder.domain.models import EligibilityResult
from scholarship_finder.domain.tri_state import (
    derive_gaps,
    evaluate_citizenship,
    evaluate_degree_field,
    evaluate_degree_level,
    evaluate_document_availability,
    evaluate_gpa_minimum,
    evaluate_language_test,
    has_blocking_unmet,
)


class TestGPAMinimumRule:
    def test_met(self):
        r = evaluate_gpa_minimum(3.7, 3.0)
        assert r.result == EligibilityResult.MET

    def test_exactly_met(self):
        r = evaluate_gpa_minimum(3.0, 3.0)
        assert r.result == EligibilityResult.MET

    def test_unmet(self):
        r = evaluate_gpa_minimum(2.5, 3.0)
        assert r.result == EligibilityResult.UNMET
        assert r.blocking is True

    def test_unknown_no_candidate(self):
        r = evaluate_gpa_minimum(None, 3.0)
        assert r.result == EligibilityResult.UNKNOWN

    def test_unknown_no_requirement(self):
        r = evaluate_gpa_minimum(3.7, None)
        assert r.result == EligibilityResult.UNKNOWN


class TestDegreeFieldRule:
    def test_overlap_found(self):
        r = evaluate_degree_field(["Computer Science"], ["Machine Learning", "Computer Science"])
        assert r.result == EligibilityResult.MET

    def test_partial_token_overlap(self):
        r = evaluate_degree_field(["Electrical Engineering"], ["Engineering", "Physics"])
        assert r.result == EligibilityResult.MET

    def test_no_overlap_gives_unknown(self):
        # Fuzzy miss → UNKNOWN (LLM will judge)
        r = evaluate_degree_field(["Fine Arts"], ["Quantum Physics"])
        assert r.result == EligibilityResult.UNKNOWN

    def test_empty_candidate_gives_unknown(self):
        r = evaluate_degree_field([], ["Computer Science"])
        assert r.result == EligibilityResult.UNKNOWN

    def test_empty_required_gives_unknown(self):
        r = evaluate_degree_field(["CS"], [])
        assert r.result == EligibilityResult.UNKNOWN


class TestLanguageTestRule:
    def test_met(self):
        r = evaluate_language_test(7.5, 6.5, "ielts")
        assert r.result == EligibilityResult.MET

    def test_exactly_met(self):
        r = evaluate_language_test(6.5, 6.5, "ielts")
        assert r.result == EligibilityResult.MET

    def test_unmet_blocking(self):
        r = evaluate_language_test(5.5, 6.5, "ielts")
        assert r.result == EligibilityResult.UNMET
        assert r.blocking is True

    def test_no_candidate_score(self):
        r = evaluate_language_test(None, 6.5, "ielts")
        assert r.result == EligibilityResult.UNKNOWN

    def test_no_required_score(self):
        r = evaluate_language_test(7.0, None, "ielts")
        assert r.result == EligibilityResult.UNKNOWN


class TestDegreeLevelRule:
    def test_masters_match(self):
        r = evaluate_degree_level("msc", "ms")
        assert r.result == EligibilityResult.MET

    def test_btech_vs_bachelors(self):
        r = evaluate_degree_level("btech", "bachelors")
        assert r.result == EligibilityResult.MET

    def test_phd_vs_masters_unmet(self):
        r = evaluate_degree_level("phd", "masters")
        assert r.result == EligibilityResult.UNMET

    def test_any_required_always_met(self):
        r = evaluate_degree_level("btech", "any")
        assert r.result == EligibilityResult.MET

    def test_missing_candidate(self):
        r = evaluate_degree_level(None, "masters")
        assert r.result == EligibilityResult.UNKNOWN


class TestAlwaysUnknownRules:
    def test_citizenship_always_unknown(self):
        r = evaluate_citizenship("Indian only")
        assert r.result == EligibilityResult.UNKNOWN

    def test_document_always_unknown(self):
        r = evaluate_document_availability()
        assert r.result == EligibilityResult.UNKNOWN


class TestHasBlockingUnmet:
    def test_no_blocking(self):
        results = [
            evaluate_gpa_minimum(3.7, 3.0),    # MET
            evaluate_citizenship(),              # UNKNOWN
        ]
        assert has_blocking_unmet(results) is False

    def test_has_blocking(self):
        results = [
            evaluate_gpa_minimum(2.0, 3.0),    # UNMET + blocking
            evaluate_citizenship(),              # UNKNOWN
        ]
        assert has_blocking_unmet(results) is True


class TestDeriveGaps:
    def test_unmet_creates_critical_gap(self):
        results = [evaluate_gpa_minimum(2.0, 3.0)]
        gaps = derive_gaps(results)
        assert len(gaps) == 1
        assert gaps[0]["blocking"] is True
        assert gaps[0]["severity"] == "CRITICAL"

    def test_unknown_creates_medium_gap(self):
        results = [evaluate_citizenship()]
        gaps = derive_gaps(results)
        assert len(gaps) == 1
        assert gaps[0]["blocking"] is False
        assert gaps[0]["severity"] == "MEDIUM"

    def test_met_creates_no_gap(self):
        results = [evaluate_gpa_minimum(3.7, 3.0)]
        gaps = derive_gaps(results)
        assert gaps == []
