"""Tests for the weighted scoring engine and ranking."""
from __future__ import annotations

import pytest

from scholarship_finder.domain.models import FundingLevel
from scholarship_finder.domain.scoring import (
    BLOCKING_UNMET_CAP,
    ScoringInput,
    compute_academic_fit,
    compute_composite_score,
    compute_funding_score,
    rank_opportunities,
)


class TestAcademicFit:
    def test_large_positive_margin(self):
        assert compute_academic_fit(2.0) == pytest.approx(100.0)

    def test_exact_minimum(self):
        score = compute_academic_fit(0.0)
        assert score == pytest.approx(65.0)

    def test_below_minimum(self):
        score = compute_academic_fit(-0.5)
        assert score < 65.0
        assert score >= 0.0

    def test_none_gives_neutral(self):
        assert compute_academic_fit(None) == pytest.approx(50.0)


class TestFundingScore:
    def test_full_tuition_stipend(self):
        assert compute_funding_score(FundingLevel.FULL_TUITION_STIPEND) == pytest.approx(100.0)

    def test_full_tuition(self):
        assert compute_funding_score(FundingLevel.FULL_TUITION) == pytest.approx(80.0)

    def test_partial(self):
        assert compute_funding_score(FundingLevel.PARTIAL) == pytest.approx(50.0)

    def test_none(self):
        assert compute_funding_score(FundingLevel.NONE) == pytest.approx(20.0)

    def test_unknown_neutral(self):
        score = compute_funding_score(FundingLevel.UNKNOWN)
        assert 30.0 <= score <= 50.0


class TestCompositeScore:
    def _good_input(self, **overrides) -> ScoringInput:
        defaults = dict(
            gpa_margin=0.5,
            field_alignment=0.8,
            funding_level=FundingLevel.FULL_TUITION_STIPEND,
            feasibility_score=0.9,
            evidence_confidence=0.85,
            has_blocking_unmet=False,
        )
        defaults.update(overrides)
        return ScoringInput(**defaults)

    def test_total_in_range(self):
        score = compute_composite_score(self._good_input())
        assert 0.0 <= score.total <= 100.0

    def test_blocking_unmet_caps(self):
        inp = self._good_input(has_blocking_unmet=True)
        score = compute_composite_score(inp)
        assert score.total <= BLOCKING_UNMET_CAP
        assert score.capped is True

    def test_no_blocking_not_capped(self):
        inp = self._good_input(has_blocking_unmet=False)
        score = compute_composite_score(inp)
        assert score.capped is False

    def test_all_components_present(self):
        score = compute_composite_score(self._good_input())
        for key in ("academic_fit", "field_alignment", "funding_strength",
                     "feasibility", "evidence_confidence"):
            assert key in score.components

    def test_custom_weights_respected(self):
        inp = self._good_input(gpa_margin=2.0)
        weights = {"academic_fit": 1.0, "field_alignment": 0.0,
                   "funding_strength": 0.0, "feasibility": 0.0, "evidence_confidence": 0.0}
        score = compute_composite_score(inp, weights=weights)
        assert score.total == pytest.approx(100.0, abs=1.0)


class TestRankOpportunities:
    def _make_score(self, total, evidence_confidence=0.8, deadline_days=90, capped=False):
        from scholarship_finder.domain.scoring import CompositeScore
        return CompositeScore(
            total=total,
            capped=capped,
            evidence_confidence=evidence_confidence,
            deadline_distance_days=deadline_days,
        )

    def test_returns_at_most_5(self):
        scores = [(f"opp{i}", self._make_score(90 - i * 5)) for i in range(10)]
        ranked = rank_opportunities(scores)
        assert len(ranked) <= 5

    def test_sorted_by_total_descending(self):
        scores = [("b", self._make_score(70)), ("a", self._make_score(90))]
        ranked = rank_opportunities(scores)
        assert ranked[0][0] == "a"
        assert ranked[0][2] == 1

    def test_tiebreak_by_evidence_confidence(self):
        scores = [
            ("low_conf", self._make_score(80, evidence_confidence=0.5)),
            ("high_conf", self._make_score(80, evidence_confidence=0.9)),
        ]
        ranked = rank_opportunities(scores)
        assert ranked[0][0] == "high_conf"

    def test_tiebreak_by_deadline(self):
        scores = [
            ("late", self._make_score(80, evidence_confidence=0.8, deadline_days=180)),
            ("soon", self._make_score(80, evidence_confidence=0.8, deadline_days=30)),
        ]
        ranked = rank_opportunities(scores)
        assert ranked[0][0] == "soon"

    def test_rank_is_1_indexed(self):
        scores = [("a", self._make_score(90))]
        ranked = rank_opportunities(scores)
        assert ranked[0][2] == 1
