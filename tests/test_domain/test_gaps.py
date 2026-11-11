"""Tests for deterministic gap aggregation."""
from scholarship_finder.domain.gaps import aggregate_gaps
from scholarship_finder.domain.models import (
    CandidateProfile, EligibilityAssessment, EligibilityResult,
    EligibilityRule, Opportunity)


def _a(oid, status, rules, rank=None):
    return EligibilityAssessment(opportunity_id=oid, status=status,
                                 rules=rules, rank=rank)


def _r(name, res, closable=True, gap=None, req=""):
    return EligibilityRule(rule_name=name, result=res, closable=closable,
                           gap_size=gap, required_value=req)


def test_cluster_unlocks_and_priority():
    opps = [Opportunity(id="a", name="A"), Opportunity(id="b", name="B")]
    asmts = [
        _a("a", "near_miss", [_r("language_ielts", EligibilityResult.UNMET, gap=0.5, req="IELTS ≥ 7")]),
        _a("b", "near_miss", [_r("language_ielts", EligibilityResult.UNMET, gap=1.0, req="IELTS ≥ 7")]),
    ]
    clusters = aggregate_gaps(asmts, opps, CandidateProfile())
    assert len(clusters) == 1
    c = clusters[0]
    assert c.category == "test" and set(c.unlocks) == {"a", "b"}
    assert c.unlock_names == ["A", "B"] and c.priority <= 2


def test_met_and_nonclosable_excluded():
    opps = [Opportunity(id="a", name="A")]
    a = _a("a", "ineligible", [
        _r("nationality", EligibilityResult.UNMET, closable=False),
        _r("gpa", EligibilityResult.MET)])
    assert aggregate_gaps([a], opps, CandidateProfile()) == []


def test_informational_and_unjudged_rules_make_no_clusters():
    opps = [Opportunity(id="a", name="A")]
    a = _a("a", "likely_eligible", [
        _r("funding_fit", EligibilityResult.UNKNOWN, closable=False),
        _r("field_of_study", EligibilityResult.UNKNOWN, closable=False),
        _r("destination", EligibilityResult.UNKNOWN, closable=False)], rank=1)
    assert aggregate_gaps([a], opps, CandidateProfile()) == []


def test_unknown_student_data_gap_is_kept_when_closable():
    opps = [Opportunity(id="a", name="A")]
    a = _a("a", "near_miss", [_r("gpa", EligibilityResult.UNKNOWN, closable=True)])
    assert [c.rule_name for c in aggregate_gaps([a], opps, CandidateProfile())] == ["gpa"]


def test_deadline_hint_ignores_passed_deadlines():
    from datetime import date, timedelta
    from scholarship_finder.domain.models import DeadlineInfo
    past = (date.today() - timedelta(days=200)).isoformat()
    soon = (date.today() + timedelta(days=20)).isoformat()
    opps = [Opportunity(id="a", name="A", deadlines=[DeadlineInfo(iso_date=past, is_past=True)]),
            Opportunity(id="b", name="B", deadlines=[DeadlineInfo(iso_date=soon, is_past=False)])]
    rule = [_r("language_ielts", EligibilityResult.UNMET, gap=0.5, req="IELTS ≥ 7")]
    only_past = aggregate_gaps([_a("a", "near_miss", rule)], opps, CandidateProfile())[0]
    assert only_past.deadline_hint == ""
    both = aggregate_gaps([_a("a", "near_miss", rule), _a("b", "near_miss", rule)], opps,
                          CandidateProfile())[0]
    assert both.deadline_hint == "20 days"


def test_target_is_requirement():
    opps = [Opportunity(id="a", name="A")]
    a = _a("a", "near_miss", [_r("language_ielts", EligibilityResult.UNMET, req="IELTS ≥ 7.0")])
    c = aggregate_gaps([a], opps, CandidateProfile())[0]
    assert c.target == "IELTS ≥ 7.0"
