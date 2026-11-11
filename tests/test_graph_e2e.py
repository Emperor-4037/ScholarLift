"""End-to-end graph test: fake LLM/search/fetch through the real graph."""

from scholarship_finder.domain.models import (
    Education, CandidateProfile, Preferences)
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.engine.gateway import make_initial_state
from scholarship_finder.engine.graph import make_graph
from scholarship_finder.ports.llm import LLMResult
from scholarship_finder.ports.search import SearchResult, SearchResultItem
from scholarship_finder.ports.fetch import FetchedDocument


def _resp(parsed=None, content=""):
    r = LLMResult(content=content, provider="f", model="m")
    r.parsed = parsed
    return r


class FakeLLM:
    async def complete(self, spec):
        return _resp(content="[CANDIDATE_NAME] is motivated [F1].")
    async def complete_json(self, spec):
        return _resp(parsed=[{"id": "x", "field_alignment": 0.8,
                              "profile_strength": 0.7, "rationale": "good"}])
    def available(self): return True


class DeadLLM(FakeLLM):
    async def complete(self, spec):
        from scholarship_finder.errors import LLMUnavailableError
        raise LLMUnavailableError("router", "down", attempts=["m:err"])
    async def complete_json(self, spec):
        from scholarship_finder.errors import LLMUnavailableError
        raise LLMUnavailableError("router", "down", attempts=["m:err"])


class FakeSearch:
    async def search(self, q):
        return SearchResult(items=[SearchResultItem(
            url=f"https://uni{i}.edu/sch", title=f"Sch{i}",
            snippet="fully funded scholarship")
            for i in range(4)], provider="f", query_text=q.text)


class FakeFetch:
    async def fetch(self, url, policy=None):
        return FetchedDocument(url=url, final_url=url, status_class="2xx",
            normalized_text="Fully Funded Award " + "x " * 400)


def _profile():
    return CandidateProfile(
        nationality="India",
        education=[Education(degree="B.Tech", field="CS")],
        preferences=Preferences(degree_level="masters", target_fields=["CS"],
                                countries=["Germany"]))


def _agents(llm=None):
    from scholarship_finder.agents.profile_enricher import ProfileEnricherAgent
    from scholarship_finder.agents.opportunity_scout import OpportunityScoutAgent
    from scholarship_finder.agents.source_auditor import SourceAuditorAgent
    from scholarship_finder.agents.ranking_strategist import RankingStrategistAgent
    from scholarship_finder.agents.gap_advisor import GapAdvisorAgent
    from scholarship_finder.agents.application_strategist import ApplicationStrategistAgent
    llm = llm or FakeLLM()
    return dict(
        profile_enricher_agent=ProfileEnricherAgent(llm, None),
        opportunity_scout_agent=OpportunityScoutAgent(llm, FakeSearch()),
        source_auditor_agent=SourceAuditorAgent(llm, FakeFetch()),
        ranking_strategist_agent=RankingStrategistAgent(llm),
        gap_advisor_agent=GapAdvisorAgent(llm),
        application_strategist_agent=ApplicationStrategistAgent(llm),
    )


async def _drive(agents, state=None):
    run = make_graph(**agents, bus=EventBus(), scrubbed_text="", target_verified=10)
    last = state or make_initial_state()
    last.profile = _profile()
    async for st in run(last, EventBus()):
        last = st
    return last


async def test_e2e_reaches_finalize():
    last = await _drive(_agents())
    assert last.run_meta.phase.value == "COMPLETED"
    assert last.run_meta.outcome.value in ("COMPLETE", "DEGRADED")


async def test_e2e_llm_down_degrades_not_crashes():
    last = await _drive(_agents(llm=DeadLLM()))
    assert last.run_meta.phase.value == "COMPLETED"
    # No SOP possible → degraded, not a crash
    assert last.run_meta.outcome.value == "DEGRADED"


async def test_e2e_terminal_event_present():
    from scholarship_finder.engine.context import RunContext, set_context, reset_context
    agents = _agents()
    bus = EventBus()
    run = make_graph(**agents, bus=bus, scrubbed_text="", target_verified=10)
    state = make_initial_state()
    state.profile = _profile()
    token = set_context(RunContext(run_id=state.run_meta.run_id, bus=bus))
    try:
        async for st in run(state, bus):
            state = st
    finally:
        reset_context(token)
    assert bus.terminal_event is not None
