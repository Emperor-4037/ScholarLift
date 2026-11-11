"""Tests for deterministic composite scoring."""
from scholarship_finder.domain.scoring import (
    WEIGHTS, score_opportunity, select_top_five)
from scholarship_finder.domain.models import (
    EligibilityAssessment, EligibilityResult, EligibilityRule, FundingInfo,
    FundingLevel, Opportunity, TrustTier, VerificationResult)


def _opp(name="X", provider="P", level=FundingLevel.FULL_TUITION_STIPEND, cid="o1"):
    return Opportunity(
        id=cid, name=name, institution_or_funder=provider,
        funding=FundingInfo(level=level),
        verification=VerificationResult(
            reachable=True, existence_verified=True,
            completeness_score=0.75, trust_tier=TrustTier.OFFICIAL_INSTITUTION))


def test_weights_sum_to_one():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_component_explanations_contain_numbers():
    comp, parts = score_opportunity(
        _opp(), status="eligible",
        rules=[EligibilityRule(rule_name="gpa", result=EligibilityResult.MET)],
        field_alignment=0.8, profile_strength=0.6,
        gpa_margin=0.5, days_to_deadline=90, evidence_confidence=0.75)
    assert 0 <= comp <= 100 and len(parts) == 7
    assert any("0.8" in c.explanation or "80" in c.explanation for c in parts)
    assert any("0.5" in c.explanation for c in parts)
    for c in parts:
        assert c.weight == WEIGHTS[c.component]
        assert abs(c.weighted - c.score * c.weight) < 0.05


def test_eligible_beats_near_miss():
    c1, _ = score_opportunity(_opp(cid="a"), status="eligible", rules=[],
                              field_alignment=.5, profile_strength=.5,
                              gpa_margin=None, days_to_deadline=None,
                              evidence_confidence=.5)
    c2, _ = score_opportunity(_opp(cid="b"), status="near_miss", rules=[],
                              field_alignment=.5, profile_strength=.5,
                              gpa_margin=None, days_to_deadline=None,
                              evidence_confidence=.5)
    assert c1 > c2


def test_provider_diversity_cap():
    opps = [_opp(name=f"O{i}", provider="SameU", cid=f"o{i}") for i in range(4)]
    opps.append(_opp(name="Other", provider="DiffU", cid="o9"))
    asmts = [EligibilityAssessment(opportunity_id=o.id, status="eligible",
                                   composite_score=90 - i)
             for i, o in enumerate(opps)]
    top = select_top_five(asmts, opps, max_per_provider=2)
    assert "o9" in top and sum(1 for x in top if x != "o9") <= 2


def test_near_miss_fills_shortfall():
    opps = [_opp(cid="a"), _opp(cid="b", name="Y"), _opp(cid="c", name="Z")]
    asmts = [
        EligibilityAssessment(opportunity_id="a", status="eligible", composite_score=80),
        EligibilityAssessment(opportunity_id="b", status="near_miss", composite_score=60),
        EligibilityAssessment(opportunity_id="c", status="ineligible", composite_score=50),
    ]
    assert select_top_five(asmts, opps) == ["a", "b"]
