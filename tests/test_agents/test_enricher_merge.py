"""One malformed LLM value must never cost the whole profile (live regression:
preferences.funding_required="unknown" degraded an entire run)."""
from scholarship_finder.agents.profile_enricher import _as_bool, _merge
from scholarship_finder.domain.models import CandidateProfile, Preferences


def _base():
    return CandidateProfile(nationality="Nigerian",
                            preferences=Preferences(degree_level="masters", countries=["UK"]))


def test_unknown_funding_required_is_ignored_not_fatal():
    out = _merge(_base(), {"preferences": {"funding_required": "unknown", "intake": "Fall 2027"},
                           "skills": ["Python"]}, {})
    assert out.skills == ["Python"] and out.preferences.intake == "Fall 2027"
    assert out.preferences.countries == ["UK"]


def test_bad_items_dropped_good_items_kept():
    ext = {"education": [{"cgpa_value": "abc"}, {"degree": "BSc", "field": "Nursing"}],
           "financial_need": {"level": ["not", "a", "string"]},
           "languages": ["English"]}
    out = _merge(_base(), ext, {})
    assert [e.degree for e in out.education] == ["BSc"]
    assert out.languages == ["English"] and out.nationality == "Nigerian"


def test_as_bool():
    assert _as_bool("Yes") is True and _as_bool("no") is False
    assert _as_bool("unknown") is None and _as_bool(None) is None and _as_bool(True) is True
