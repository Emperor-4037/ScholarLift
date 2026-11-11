from scholarship_finder.agents.opportunity_scout import OpportunityScoutAgent, _template_queries
from scholarship_finder.domain.models import CandidateProfile, Preferences, SwarmState
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.llm import LLMResult
from scholarship_finder.ports.search import SearchResult, SearchResultItem


class FakeLLM:
    async def complete_json(self, spec):
        r = LLMResult(content="", provider="f", model="m"); r.parsed = []; return r


class FakeSearch:
    async def search(self, q):
        return SearchResult(items=[SearchResultItem(url="https://uni.edu/award",
            title="Award", snippet="full scholarship")], provider="f", query_text=q.text)


def _profile():
    return CandidateProfile(nationality="Nigerian",
        preferences=Preferences(degree_level="masters",
            target_fields=["Public Health"], countries=["UK", "Australia", "Sweden"]))


def test_fallback_queries_cover_each_country_and_neutral():
    qs = _template_queries(_profile())
    blob = " ".join(q.text.lower() for q in qs)
    for c in ("uk", "australia", "sweden"):
        assert any(c == (q.country or "").lower() or c in blob for q in qs)
    for banned in ("computer science", "mit", "stanford"):
        assert banned not in blob


async def test_complement_covers_countries_and_nationality():
    agent = OpportunityScoutAgent(FakeLLM(), FakeSearch())
    plan = await agent._plan(_profile())
    for c in ("uk", "australia", "sweden"):
        assert any((q.country or "").lower() == c or c in q.text.lower() for q in plan.queries)
    assert sum("nigerian" in q.text.lower() for q in plan.queries) >= 2


class DownSearch:
    async def search(self, q):
        raise RuntimeError("provider down")


class RecordingSearch(FakeSearch):
    def __init__(self):
        self.queries = []

    async def search(self, q):
        self.queries.append(q)
        return await super().search(q)


async def test_resolve_falls_back_to_secondary_when_primary_down():
    agent = OpportunityScoutAgent(FakeLLM(), DownSearch(), FakeSearch())
    hits = await agent._resolve([{"name": "Uni Award", "provider": "Uni"}], None)
    assert [h["url"] for h in hits] == ["https://uni.edu/award"]


async def test_execute_falls_back_and_passes_include_domains():
    from scholarship_finder.domain.models import SearchQuery as DQ
    rec = RecordingSearch()
    agent = OpportunityScoutAgent(FakeLLM(), DownSearch(), rec)
    items = await agent._execute([DQ(text="masters scholarship", include_domains=["daad.de"])])
    assert items and rec.queries[0].include_domains == ["daad.de"]
    both_down = OpportunityScoutAgent(FakeLLM(), DownSearch(), DownSearch())
    assert await both_down._execute([DQ(text="x")]) == []


async def test_propose_drops_programs_hosted_outside_target_countries():
    class LLM(FakeLLM):
        async def complete_json(self, spec):
            r = LLMResult(content="", provider="f", model="m")
            r.parsed = [
                {"name": "Chevening Awards", "host_country": "United Kingdom", "degree_level": "masters"},
                {"name": "ICCR Inbound Scholarship", "host_country": "India", "degree_level": "masters"},
                {"name": "Global Award", "host_country": "", "degree_level": "masters"}]
            return r

    agent = OpportunityScoutAgent(LLM(), FakeSearch())
    prof = CandidateProfile(nationality="Indian", preferences=Preferences(
        degree_level="masters", countries=["UK", "United Kingdom", "Germany"]))
    names = [d["name"] for d in await agent._propose(prof, None)]
    assert names == ["Chevening Awards", "Global Award"]


async def test_run_produces_candidates():
    agent = OpportunityScoutAgent(FakeLLM(), FakeSearch())
    out = await agent.run(SwarmState(profile=_profile()), EventBus())
    assert out["candidates"]
