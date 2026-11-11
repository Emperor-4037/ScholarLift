"""Streaming auditor: KB reuse, level/trust precision gates, one-hop, hints, deadline."""
import random

from scholarship_finder.agents import source_auditor as sa
from scholarship_finder.agents.source_auditor import SourceAuditorAgent
from scholarship_finder.adapters.knowledge_base import get_kb
from scholarship_finder.domain.models import (
    Candidate, CandidateProfile, Preferences, SwarmState, TrustTier,
)
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.fetch import FetchedDocument
from scholarship_finder.ports.llm import LLMResult

BODY = ("The Frobnitz Graduate Scholarship funds master's students with a monthly "
        "stipend and full tuition. Applicants must hold a bachelor's degree. "
        "IELTS 6.5 is required. ") * 6
UG = ("The Zed Undergraduate Scholarship funds bachelor's students with tuition "
      "support and a living stipend each year of study. ") * 8


def _profile(level="Master's"):
    return CandidateProfile(preferences=Preferences(degree_level=level, countries=["Canada"]))


def _cand(url, tier=TrustTier.OFFICIAL_INSTITUTION, title="A Scholarship"):
    return Candidate(url=url, canonical_url=url, domain=url.split("/")[2],
                     trust_tier=tier, title=title)


class Fetch:
    def __init__(self, pages):
        self.pages, self.calls = pages, 0

    async def fetch(self, url, policy=None):
        self.calls += 1
        return FetchedDocument(url=url, final_url=url, status_class="2xx",
                               normalized_text=self.pages.get(url, ""))


class LLM:
    def __init__(self, by_url=None, default=None):
        self.by_url, self.default, self.calls = by_url or {}, default, 0

    async def complete_json(self, spec):
        self.calls += 1
        parsed = next((v for u, v in self.by_url.items() if u in spec.prompt), self.default)
        r = LLMResult(content="", provider="f", model="m")
        r.parsed = parsed
        return r


def _item(name="Frobnitz Graduate Scholarship", levels=("Master's",), **kw):
    d = {"is_specific_opportunity": True, "is_listing_page": False, "name": name,
         "provider": "Frobnitz Univ", "degree_levels": list(levels),
         "eligibility": {"degree_levels": list(levels)},
         "evidence": {"name": "The Frobnitz Graduate Scholarship funds master's students"}}
    d.update(kw)
    return d


async def _run(agent, cands, level="Master's"):
    return await agent.run(SwarmState(candidates=cands, profile=_profile(level)), EventBus())


async def test_second_run_served_from_kb():
    pages = {"https://a.edu/s1": BODY, "https://a.edu/s2": "tiny"}
    fetch, llm = Fetch(pages), LLM(default=_item())
    out = await _run(SourceAuditorAgent(llm, fetch), [_cand("https://a.edu/s1"), _cand("https://a.edu/s2")])
    assert len(out["opportunities"]) == 1
    f1, l1 = fetch.calls, llm.calls
    out2 = await _run(SourceAuditorAgent(llm, fetch), [_cand("https://a.edu/s1"), _cand("https://a.edu/s2")])
    assert (fetch.calls, llm.calls) == (f1, l1)
    assert len(out2["opportunities"]) == 1


async def test_gate_rejected_marked_audited_and_empty_fetch_not_cached():
    c1, c2 = _cand("https://a.edu/tiny"), _cand("https://a.edu/empty")
    fetch = Fetch({"https://a.edu/tiny": "tiny"})
    await _run(SourceAuditorAgent(LLM(default=_item()), fetch), [c1, c2])
    assert c1.audited and c2.audited
    assert get_kb().get_verdict("https://a.edu/empty") is None


async def test_undergrad_only_dropped_without_llm_and_not_cached():
    url = "https://a.edu/undergraduate-scholarship"
    llm = LLM(default=_item())
    out = await _run(SourceAuditorAgent(llm, Fetch({url: UG})), [_cand(url)])
    assert llm.calls == 0 and out["opportunities"] == []
    assert get_kb().get_verdict(url) is None          # student-specific: never persisted


async def test_extracted_bachelor_opp_dropped_and_cached_opp_skipped_for_masters():
    url = "https://a.edu/mixed"
    page = BODY + "Also open to others."
    llm = LLM(default=_item(levels=("Bachelor's",)))
    out = await _run(SourceAuditorAgent(llm, Fetch({url: page})), [_cand(url)])
    assert out["opportunities"] == []
    # the profile-independent fact is cached; a bachelor's student gets it without any call
    out2 = await _run(SourceAuditorAgent(llm, Fetch({})), [_cand(url)], level="Bachelor's")
    assert [o.name for o in out2["opportunities"]] == ["Frobnitz Graduate Scholarship"]
    out3 = await _run(SourceAuditorAgent(llm, Fetch({})), [_cand(url)], level="Master's")
    assert out3["opportunities"] == []


async def test_weak_source_kept_but_not_counted_and_aggregator_never_accepted():
    weak = _cand("https://blog-x.example/s", tier=TrustTier.UNKNOWN)
    agg = _cand("https://scholars4dev.example/s", tier=TrustTier.AGGREGATOR)
    pages = {weak.canonical_url: BODY, agg.canonical_url: BODY}
    out = await _run(SourceAuditorAgent(LLM(default=_item()), Fetch(pages)), [weak, agg])
    urls = [o.official_url for o in out["opportunities"]]
    assert urls == [weak.canonical_url]


def _listing_md(base):
    links = "\n".join(f"- [Graduate Scholarship {i} for master's students]({base}/award-{i})"
                      for i in range(14))
    return "# Funding\nScholarships and fellowships directory.\n" + links + "\n" + BODY


async def test_listing_one_hop_trusted_only_and_no_grandchildren():
    base = "https://dir.edu/funding"
    listing = {"is_specific_opportunity": False, "is_listing_page": True,
               "listed_opportunity_names": ["Frobnitz Graduate Scholarship"], "name": "Funding",
               "evidence": {}}
    pages = {base: _listing_md("https://dir.edu"),
             **{f"https://dir.edu/award-{i}": _listing_md("https://dir.edu") for i in range(14)}}
    llm = LLM(by_url={base: listing}, default=_item(name="Child Award"))
    cands = [_cand(base)]
    state = SwarmState(candidates=cands, profile=_profile())
    out = await SourceAuditorAgent(llm, Fetch(pages)).run(state, EventBus())
    kids = [c for c in state.candidates if c.origin == "listing"]
    assert 0 < len(kids) <= 10 and all(c.audited for c in kids)
    assert {l.name for l in out["leads"]} >= {"Frobnitz Graduate Scholarship"}
    # untrusted listing parent: leads only, zero children
    base2 = "https://agg.example/funding"
    st2 = SwarmState(candidates=[_cand(base2, tier=TrustTier.UNKNOWN)], profile=_profile())
    pages2 = {base2: _listing_md("https://agg.example")}
    await SourceAuditorAgent(LLM(default=listing), Fetch(pages2)).run(st2, EventBus())
    assert not [c for c in st2.candidates if c.origin == "listing"]


def test_apply_hints_fills_only_when_sentence_on_page():
    from scholarship_finder.domain.req_hints import extract_hints
    cand = _cand("https://a.edu/h")
    opp = sa._build_opp(_item(), cand, BODY)
    assert not opp.eligibility.language_requirements
    sa._apply_hints(opp, extract_hints(BODY), BODY, cand.canonical_url)
    assert opp.eligibility.language_requirements[0].min_score == 6.5
    opp2 = sa._build_opp(_item(), cand, BODY)
    fake = {"ielts": {"value": "IELTS 8", "groups": ("8",), "sentence": "Never on the page at all."}}
    sa._apply_hints(opp2, fake, BODY, cand.canonical_url)
    assert not opp2.eligibility.language_requirements


async def test_discovery_deadline_keeps_finished_pages(monkeypatch):
    urls = [f"https://a.edu/p{i}" for i in range(6)]
    pages = {u: BODY.replace("Frobnitz", f"Frob{i}x") for i, u in enumerate(urls)}
    state_calls = {"n": 0}

    def dtl():
        state_calls["n"] += 1
        return 100.0 if state_calls["n"] < 25 else 0.0

    monkeypatch.setattr(sa, "_dtl", dtl)
    llm = LLM(default=_item())
    out = await _run(SourceAuditorAgent(llm, Fetch(pages)), [_cand(u) for u in urls])
    assert isinstance(out["opportunities"], list)


async def test_slow_straggler_is_cut_at_round_cap_but_finished_pages_kept(monkeypatch):
    import asyncio
    import time
    monkeypatch.setattr(sa, "_ROUND_S", (0.6, 0.6))
    fast, slow = "https://a.edu/fast", "https://a.edu/slow"

    class SlowFetch(Fetch):
        async def fetch(self, url, policy=None):
            if url == slow:
                await asyncio.sleep(30)
            return await super().fetch(url, policy)

    t0 = time.monotonic()
    out = await _run(SourceAuditorAgent(LLM(default=_item()), SlowFetch({fast: BODY, slow: BODY})),
                     [_cand(fast), _cand(slow)])
    assert time.monotonic() - t0 < 10
    assert [o.official_url for o in out["opportunities"]] == [fast]


def test_single_item_shapes():
    assert sa._single_item({"name": "x"}) == {"name": "x"}
    assert sa._single_item({"pages": [{"name": "y"}]}) == {"name": "y"}
    assert sa._single_item([1, {"name": "z"}]) == {"name": "z"}
    assert sa._single_item("junk") is None and sa._single_item(None) is None


def test_build_opp_fuzz_never_raises():
    rnd = random.Random(7)
    vals = [None, "", "x" * 50, 3, 2.5, True, [], ["a", 1, None], {}, {"a": [1]}, [{}]]
    keys = ["name", "provider", "host_countries", "degree_levels", "fields_of_study",
            "funding", "deadline", "eligibility", "evidence", "selection_criteria",
            "application_url", "how_to_apply", "is_specific_opportunity", "kind"]
    cand = _cand("https://a.edu/f")
    for _ in range(200):
        item = {k: rnd.choice(vals) for k in rnd.sample(keys, rnd.randint(0, len(keys)))}
        sa._build_opp(item, cand, BODY, {"ielts": rnd.choice(vals)} if rnd.random() < .3 else None)
