"""Tests for deterministic eligibility rules."""
from datetime import date, timedelta
from scholarship_finder.domain.eligibility import classify_status, evaluate_rules
from scholarship_finder.domain.models import (
    CandidateProfile, DeadlineInfo, Education, EligibilityCriteria,
    EligibilityResult, LanguageRequirement, LanguageTest, NormalizedGPA,
    Opportunity, Preferences)


def _profile():
    return CandidateProfile(
        nationality="India",
        education=[Education(
            degree="B.Tech", field="CS", cgpa_value=8.0, cgpa_scale=10.0,
            normalized_gpa=NormalizedGPA(value=3.6))],
        language_tests=[LanguageTest(test_name="IELTS", overall_score=7.0)],
        preferences=Preferences(degree_level="masters", target_fields=["CS"]))


def test_nationality_alias_match():
    opp = Opportunity(eligibility=EligibilityCriteria(nationalities=["Indian"]))
    r = next(x for x in evaluate_rules(_profile(), opp) if x.rule_name == "nationality")
    assert r.result == EligibilityResult.MET


def test_nationality_unmet_blocking():
    opp = Opportunity(eligibility=EligibilityCriteria(nationalities=["German"]))
    rules = evaluate_rules(_profile(), opp)
    r = next(x for x in rules if x.rule_name == "nationality")
    assert r.result == EligibilityResult.UNMET and r.blocking
    assert classify_status(rules) == "ineligible"


def test_open_to_international_passes():
    opp = Opportunity(eligibility=EligibilityCriteria(open_to_international=True))
    r = next(x for x in evaluate_rules(_profile(), opp) if x.rule_name == "nationality")
    assert r.result == EligibilityResult.MET


def test_gpa_margin_in_explanation():
    opp = Opportunity(eligibility=EligibilityCriteria(min_gpa_value=3.0, min_gpa_scale=4.0))
    r = next(x for x in evaluate_rules(_profile(), opp) if x.rule_name == "gpa")
    assert r.result == EligibilityResult.MET and "0.6" in r.explanation


def test_gpa_short_near_miss():
    p = _profile()
    p.education[0].normalized_gpa = NormalizedGPA(value=2.9)
    opp = Opportunity(eligibility=EligibilityCriteria(min_gpa_value=3.1, min_gpa_scale=4.0))
    rules = evaluate_rules(p, opp)
    r = next(x for x in rules if x.rule_name == "gpa")
    assert r.result == EligibilityResult.UNMET and r.closable and r.gap_size == 0.2
    assert classify_status(rules) == "near_miss"


def test_language_same_test_met():
    opp = Opportunity(eligibility=EligibilityCriteria(
        language_requirements=[LanguageRequirement(test="IELTS", min_score=6.5)]))
    r = next(x for x in evaluate_rules(_profile(), opp) if x.rule_name == "language_ielts")
    assert r.result == EligibilityResult.MET


def test_missing_test_unmet_closable():
    p = _profile()
    p.language_tests = []
    opp = Opportunity(eligibility=EligibilityCriteria(
        language_requirements=[LanguageRequirement(test="IELTS", min_score=6.5)]))
    r = next(x for x in evaluate_rules(p, opp) if x.rule_name == "language_ielts")
    assert r.result == EligibilityResult.UNMET and r.closable and not r.blocking


def test_past_deadline_blocking():
    past = (date.today() - timedelta(days=10)).isoformat()
    opp = Opportunity(deadlines=[DeadlineInfo(iso_date=past, is_past=True)])
    r = next(x for x in evaluate_rules(_profile(), opp) if x.rule_name == "deadline")
    assert r.result == EligibilityResult.UNMET and r.blocking


def test_missing_nationality_unknown():
    p = _profile()
    p.nationality = ""
    opp = Opportunity(eligibility=EligibilityCriteria(nationalities=["Indian"]))
    r = next(x for x in evaluate_rules(p, opp) if x.rule_name == "nationality")
    assert r.result == EligibilityResult.UNKNOWN


def _prof(countries):
    from scholarship_finder.domain.models import CandidateProfile, Preferences
    return CandidateProfile(
        nationality="Indian",
        preferences=Preferences(degree_level="masters", target_fields=["CS"],
                                countries=countries))


def test_destination_on_target():
    from scholarship_finder.domain.eligibility import evaluate_rules
    from scholarship_finder.domain.models import (
        EligibilityResult, Opportunity)
    opp = Opportunity(name="X", official_url="https://x.de/s",
                      host_countries=["Germany"])
    rules = evaluate_rules(_prof(["Canada", "Germany"]), opp)
    d = next(r for r in rules if r.rule_name == "destination")
    assert d.result == EligibilityResult.MET


def test_destination_off_target_blocking():
    from scholarship_finder.domain.eligibility import evaluate_rules, classify_status
    from scholarship_finder.domain.models import Opportunity
    opp = Opportunity(name="X", official_url="https://x.edu/s",
                      host_countries=["USA"],
                      eligibility=__import__("scholarship_finder.domain.models", fromlist=["EligibilityCriteria"]).EligibilityCriteria(
                          degree_levels=["masters"]))
    rules = evaluate_rules(_prof(["Canada", "Germany"]), opp)
    d = next(r for r in rules if r.rule_name == "destination")
    assert d.result == EligibilityResult.UNMET and d.blocking
    assert classify_status(rules) == "ineligible"


def test_destination_unknown_when_unstated():
    from scholarship_finder.domain.eligibility import evaluate_rules
    from scholarship_finder.domain.models import Opportunity
    opp = Opportunity(name="X", official_url="https://x.edu/s")
    rules = evaluate_rules(_prof(["Canada"]), opp)
    d = next(r for r in rules if r.rule_name == "destination")
    assert d.result == EligibilityResult.UNKNOWN


def test_gaps_cluster_from_ineligible_closable():
    from scholarship_finder.domain.gaps import aggregate_gaps
    from scholarship_finder.domain.models import (
        EligibilityAssessment, EligibilityRule, EligibilityResult, Opportunity)
    opp = Opportunity(name="Ch", official_url="https://ch.uk/s")
    a = EligibilityAssessment(
        opportunity_id=opp.id, status="ineligible", composite_score=40,
        rules=[EligibilityRule(
            rule_name="language_ielts", label="Language: IELTS",
            result=EligibilityResult.UNMET, candidate_value="6.0",
            required_value="IELTS ≥ 6.5", blocking=False, closable=True,
            gap_size=0.5)])
    clusters = aggregate_gaps([a], [opp], None)
    assert clusters and clusters[0].rule_name == "language_ielts"
    assert clusters[0].unlock_names == ["Ch"]


def test_judgement_null_values_use_fallback():
    """_num coerces null LLM judgement values to the keyword fallback."""
    from scholarship_finder.agents.ranking_strategist import _num
    assert _num(None, 0.7) == 0.7
    assert _num(0.3, 0.7) == 0.3
    assert _num("0.4", 0.7) == 0.7      # non-numeric str → default, not crash


# ── level / destination / field regression tests (live-run fixes) ─────────────

def test_level_possessive_and_synonyms():
    """'Master's degree', 'Graduate', 'postgraduate' must map to masters."""
    from scholarship_finder.domain.eligibility import _level, _levels
    for s in ["Master's", "Master’s degree", "Masters", "MSc", "postgraduate"]:
        assert _levels(s) == {"masters"}, s
    assert _level("masters") == "masters"
    assert "masters" in _levels("Graduate")          # graduates covers masters (+phd)
    assert _levels("PhD") == {"phd"}


def test_degree_level_masters_met():
    from scholarship_finder.domain.models import EligibilityCriteria, Opportunity
    opp = Opportunity(eligibility=EligibilityCriteria(degree_levels=["Master's degree"]))
    r = next(x for x in evaluate_rules(_profile(), opp)
             if x.rule_name == "degree_level")
    assert r.result == EligibilityResult.MET


def test_degree_level_doctoral_only_unmet():
    from scholarship_finder.domain.models import EligibilityCriteria, Opportunity
    opp = Opportunity(eligibility=EligibilityCriteria(degree_levels=["Doctoral (PhD)"]))
    r = next(x for x in evaluate_rules(_profile(), opp)
             if x.rule_name == "degree_level")
    assert r.result == EligibilityResult.UNMET and r.blocking


def test_destination_multiword_host():
    from scholarship_finder.domain.models import Opportunity
    opp = Opportunity(host_countries=["Germany, around the world"])
    d = next(r for r in evaluate_rules(_prof(["Germany"]), opp)
             if r.rule_name == "destination")
    assert d.result == EligibilityResult.MET


def test_destination_country_alias_uk():
    from scholarship_finder.domain.models import Opportunity
    opp = Opportunity(host_countries=["UK"])
    d = next(r for r in evaluate_rules(_prof(["United Kingdom"]), opp)
             if r.rule_name == "destination")
    assert d.result == EligibilityResult.MET


def test_destination_generic_host_unknown_not_unmet():
    from scholarship_finder.domain.models import Opportunity
    opp = Opportunity(host_countries=["Worldwide"])
    d = next(r for r in evaluate_rules(_prof(["Canada"]), opp)
             if r.rule_name == "destination")
    assert d.result == EligibilityResult.UNKNOWN and not d.blocking


def test_field_open_to_all_met():
    from scholarship_finder.domain.models import EligibilityCriteria, Opportunity
    opp = Opportunity(eligibility=EligibilityCriteria(
        fields_of_study=["Open to all disciplines"]))
    r = next(x for x in evaluate_rules(_profile(), opp)
             if x.rule_name == "field_of_study")
    assert r.result == EligibilityResult.MET


def test_field_no_overlap_is_unknown_not_unmet():
    from scholarship_finder.domain.models import EligibilityCriteria, Opportunity
    opp = Opportunity(eligibility=EligibilityCriteria(
        fields_of_study=["Marine Biology"]))
    r = next(x for x in evaluate_rules(_profile(), opp)
             if x.rule_name == "field_of_study")
    assert r.result == EligibilityResult.UNKNOWN and not r.blocking


def test_past_deadline_closable_near_miss():
    from scholarship_finder.domain.models import DeadlineInfo, EligibilityCriteria, Opportunity
    past = (date.today() - timedelta(days=30)).isoformat()
    opp = Opportunity(
        deadlines=[DeadlineInfo(iso_date=past)],
        eligibility=EligibilityCriteria(open_to_international=True,
                                        degree_levels=["masters"],
                                        fields_of_study=["Computer Science"]))
    rules = evaluate_rules(_profile(), opp)
    d = next(r for r in rules if r.rule_name == "deadline")
    assert d.result == EligibilityResult.UNMET and d.closable
    assert classify_status(rules) == "near_miss"
