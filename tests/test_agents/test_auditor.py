from datetime import date, timedelta
from scholarship_finder.agents.source_auditor import SourceAuditorAgent
from scholarship_finder.domain.models import Candidate, FieldStatus, SwarmState, TrustTier
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.fetch import FetchedDocument
from scholarship_finder.ports.llm import LLMResult

PAGE = "Fully Funded Excellence Scholarship 2026 at TU Example. " * 20


def _resp(parsed):
    r = LLMResult(content="", provider="f", model="m"); r.parsed = parsed; return r


class FakeFetch:
    async def fetch(self, url, policy=None):
        return FetchedDocument(url=url, final_url=url, status_class="2xx", normalized_text=PAGE)


def _llm(parsed):
    class L:
        async def complete_json(self, spec): return _resp(parsed)
    return L()


def _cand(url="https://tu.edu/sch"):
    return Candidate(url=url, canonical_url=url, domain="tu.edu",
                     trust_tier=TrustTier.OFFICIAL_INSTITUTION)


async def test_extraction_evidenced_fields():
    agent = SourceAuditorAgent(_llm({"pages": [{"page": 0,
        "is_specific_opportunity": True, "name": "Excellence Scholarship",
        "provider": "TU Example", "deadline": {"iso_date": (date.today()+timedelta(days=30)).isoformat()},
        "eligibility": {}, "evidence": {"name": "Fully Funded Excellence Scholarship 2026"}}]}), FakeFetch())
    out = await agent.run(SwarmState(candidates=[_cand()]), EventBus())
    opp = out["opportunities"][0]
    assert opp.is_existence_verified()
    assert any(f.status == FieldStatus.EVIDENCED for f in opp.fields)
    assert opp.deadlines[0].is_past is False


async def test_listing_page_becomes_leads():
    agent = SourceAuditorAgent(_llm({"pages": [{"page": 0,
        "is_specific_opportunity": False, "is_listing_page": True,
        "listed_opportunity_names": ["DAAD EPOS", "Chevening"]}]}), FakeFetch())
    out = await agent.run(SwarmState(candidates=[_cand()]), EventBus())
    assert out["opportunities"] == []
    assert {l.name for l in out["leads"]} == {"DAAD EPOS", "Chevening"}


async def test_past_deadline_flag():
    past = (date.today() - timedelta(days=5)).isoformat()
    agent = SourceAuditorAgent(_llm({"pages": [{"page": 0,
        "is_specific_opportunity": True, "name": "Excellence Scholarship",
        "deadline": {"iso_date": past}, "eligibility": {}, "evidence": {}}]}), FakeFetch())
    out = await agent.run(SwarmState(candidates=[_cand()]), EventBus())
    assert out["opportunities"][0].deadlines[0].is_past is True
