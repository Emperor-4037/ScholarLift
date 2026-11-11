"""Agent 2: Opportunity Scout — profile-driven search planning + execution.

One FAST complete_json plans ~8 queries (intent + country). A deterministic
complement guarantees coverage: ≥1 query per target country (≤4) and ≥2
nationality-specific queries. Fallback = templates only (discipline-agnostic).
Candidates get trust tier + prior_score; top ``max_audit_candidates`` returned.

``expand(state)``: one round over auditor-discovered leads + broader queries.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import urlparse

from scholarship_finder.domain.models import (
    Candidate,
    LLMTier,
    SearchPlan,
    SearchQuery,
    SwarmState,
    TrustTier,
)
from scholarship_finder.domain.trust import classify_domain
from scholarship_finder.domain.url import canonicalize_url, extract_domain
from scholarship_finder.engine.context import emit
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.llm import LLMPort, TaskSpec
from scholarship_finder.ports.search import SearchPort, SearchQuery as PortQuery

logger = logging.getLogger(__name__)

_NUM_LLM_QUERIES = 8
_MAX_LEADS = 8
_FUNDING_WORDS = re.compile(
    r"scholarship|fellowship|stipend|funding|tuition.?waiver|bursar|grant|"
    r"stipendium|bourse|beca|beurs|fellowship|assistantship",
    re.I,
)
_INDEX_HINT = re.compile(r"(/scholarships/?$|/search|/list|directory|database)", re.I)

def _ctx():
    try:
        from scholarship_finder.engine import context as rc
        return rc.current()
    except Exception:
        return None


_STOP_TOKENS = frozenset({
    "the","a","an","of","for","in","and","to","scholarship","scholarships",
    "award","awards","program","programme","fund","fellowship","grant",
    "international","students","student","fully","funded","masters","phd"})


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def q_text(name: str, d: dict) -> str:
    return f'"{name}" {d.get("provider") or ""} scholarship'.strip()




_TLD_HINTS: dict[str, list[str]] = {
    "canada": ["*.gc.ca", "*.ca"], "germany": ["*.de"],
    "singapore": ["*.sg"], "uk": ["*.ac.uk", "*.gov.uk"],
    "united kingdom": ["*.ac.uk", "*.gov.uk"], "australia": ["*.edu.au", "*.gov.au"],
    "usa": ["*.edu", "*.gov"], "united states": ["*.edu", "*.gov"],
}
_TLD_FALLBACK = ["*.edu", "*.gov", "*.org"]

_PROPOSE_PROMPT = """List up to 30 REAL, currently-running named scholarships or funded
programs for this student, as a JSON array:
[{{"name": str, "provider": str, "host_country": str,
"official_domain_hint": "registrable domain you believe is official or empty",
"degree_level": str, "why": str}}]
Cover each target country, nationality-specific programs, and home-country
outbound schemes. ONLY programs you are confident actually exist — never invent.
Constraints: must accept applicants moving from the student's CURRENT level to
the TARGET level (e.g. bachelors→masters); do not list doctoral-only or
undergraduate-only schemes; must be open to the student's nationality or
international applicants; give the exact degree_level the scheme funds.
host_country must be one of the student's target countries (or the funder
must fund study in them). Discipline-agnostic. No commentary, JSON only.

Student: {profile}"""


_PLAN_PROMPT = """You plan web searches to find REAL, currently open funded study
opportunities (scholarships, funded programs, fellowships) for this student:
{profile}

Return a JSON array of exactly {n} objects: {{"text": "search query",
"intent": "government_scholarship|university_scholarship|funded_program|field_fellowship|need_based",
"country": "one target country or empty"}}.
Rules: concrete queries naming the country + funding type; cover different
intents and each target country; include 2 queries aimed at the student's
nationality; no aggregator sites; keep each query under 12 words."""


class OpportunityScoutAgent:
    def __init__(self, llm_port: LLMPort, search_port: SearchPort,
                 secondary_search: SearchPort | None = None) -> None:
        self._llm = llm_port
        self._search = search_port
        self._secondary = secondary_search

    # ── main node ────────────────────────────────────────────────────────────

    async def run(self, state: SwarmState, bus: EventBus,
                  scrubbed_text: str | None = None) -> dict:
        profile = state.profile
        plan = await self._plan(profile)
        emit("opportunity_scout",
             f"Search plan: {len(plan.queries)} queries ({plan.planner})")

        plan.queries = (plan.queries
                        + self._registry_queries(profile)
                        + self._official_channel_queries(profile))
        items = await self._execute(plan.queries)
        proposed = await self._propose(profile, bus)
        items += await self._resolve(proposed, bus)
        candidates = self._to_candidates(items)
        emit("opportunity_scout",
             f"{len(items)} hits → {len(candidates)} unique candidates",
             candidates=len(candidates))

        ranked = sorted(candidates, key=lambda c: -c.prior_score)
        limit = _max_audit()
        kept = ranked[:limit]
        for c in kept:
            c.audited = False
        emit("opportunity_scout",
             f"Top {len(kept)} candidates sent to audit "
             f"(best domain trust={kept[0].trust_tier.value if kept else '—'})")
        return {
            "search_plans": state.search_plans + [plan],
            "candidates": state.candidates + kept,
        }

    async def expand(self, state: SwarmState) -> dict:
        """One expansion round over uncovered leads (+ broader queries if thin)."""
        ctx = _ctx()
        if ctx is not None and (ctx.elapsed() >= 150
                                or ctx.discovery_time_left() <= 60):
            emit("opportunity_scout", "Expansion skipped — discovery budget spent")
            return {"expansion_rounds": 1}
        covered = {o.name.lower() for o in state.opportunities}
        leads = [l for l in state.leads if l.name.lower() not in covered][: _MAX_LEADS]
        queries: list[SearchQuery] = []
        for lead in leads:
            queries.append(SearchQuery(
                text=f'"{lead.name}" official scholarship',
                intent="university_scholarship",
                depth="basic", max_results=4, round_number=1,
            ))
        if len(state.candidates) < 10 and state.profile:
            for country in state.profile.preferences.countries[:3]:
                queries.append(SearchQuery(
                    text=f"scholarships {country} international students application",
                    intent="government_scholarship", country=country,
                    depth="basic", max_results=6, round_number=1,
                ))
        if not queries:
            return {}
        emit("lead_expansion", f"Expanding {len(leads)} leads "
                               f"(+{len(queries) - len(leads)} broader queries)")
        items = await self._execute(queries)
        new_cands = self._to_candidates(items)
        existing = {c.canonical_url for c in state.candidates}
        new_cands = [c for c in new_cands if c.canonical_url not in existing]
        emit("lead_expansion", f"+{len(new_cands)} new candidates from expansion")
        return {"candidates": state.candidates + new_cands}

    # ── planning ─────────────────────────────────────────────────────────────

    async def _plan(self, profile) -> SearchPlan:
        queries: list[SearchQuery] = []
        planner = "fallback"
        if profile is not None:
            try:
                res = await self._llm.complete_json(TaskSpec(
                    tier=LLMTier.FAST,
                    system=_PLAN_PROMPT.format(n=_NUM_LLM_QUERIES,
                                               profile=_profile_brief(profile)),
                    max_output_tokens=1200, purpose="query_planning",
                ))
                parsed = res.parsed
                if isinstance(parsed, dict):
                    parsed = next((v for v in parsed.values() if isinstance(v, list)), [])
                for q in parsed or []:
                    if isinstance(q, dict) and q.get("text"):
                        queries.append(SearchQuery(
                            text=str(q["text"])[:200],
                            intent=str(q.get("intent") or "university_scholarship"),
                            country=q.get("country") or None,
                            depth=_depth_for(q.get("intent")),
                            max_results=8,
                        ))
                if queries and profile is not None and profile.preferences.countries:
                    allowed = {c.lower() for c in profile.preferences.countries}
                    queries = [q for q in queries
                               if not q.country or q.country.lower() in allowed]
                if queries:
                    planner = "llm"
            except Exception as exc:
                emit("opportunity_scout",
                     f"LLM query planning failed ({type(exc).__name__}) — template fallback")
        queries = self._complement(profile, queries)
        return SearchPlan(queries=queries, planner=planner)

    def _complement(self, profile, queries: list[SearchQuery]) -> list[SearchQuery]:
        """Guarantee ≥1 query per target country (≤4) + ≥2 nationality queries."""
        countries = (profile.preferences.countries if profile else [])[:4]
        nat = (profile.nationality if profile else "") or ""
        have = {((q.country or "").lower()) for q in queries}
        nat_hits = sum(1 for q in queries if nat and nat.lower() in q.text.lower())
        for c in countries:
            if c.lower() not in have:
                queries.append(SearchQuery(
                    text=f"{c} government scholarship international students",
                    intent="government_scholarship", country=c,
                    depth="advanced", max_results=8,
                ))
        while nat and nat_hits < 2:
            queries.append(SearchQuery(
                text=f"{nat} students scholarship {countries[0] if countries else ''} fully funded",
                intent="need_based", depth="advanced", max_results=8,
            ))
            nat_hits += 1
        if not queries:
            queries = _template_queries(profile)
        return queries[:12]

    # ── proposer: LLM-proposed named programs → leads ───────────────────────

    async def _propose(self, profile, bus) -> list[dict]:
        """One REASONING complete_json → up to 30 real named programs."""
        if profile is None:
            return []
        try:
            res = await self._llm.complete_json(TaskSpec(
                tier=LLMTier.REASONING,
                system=_PROPOSE_PROMPT.format(profile=_profile_brief(profile)),
                max_output_tokens=2500, purpose="program_proposal"))
            parsed = res.parsed
            if isinstance(parsed, dict):
                parsed = next((v for v in parsed.values() if isinstance(v, list)), [])
            out = [d for d in (parsed or []) if isinstance(d, dict) and d.get("name")]
            target = (profile.preferences.degree_level or "").lower()
            BAD = {"masters": ("doctor", "phd", "postdoc", "undergrad", "bachelor"),
                   "phd": ("undergrad", "bachelor"),
                   "bachelors": ("doctor", "phd", "postdoc", "masters")}
            if target:
                bad = BAD.get(target, ())
                out = [d for d in out
                       if not any(b in str(d.get("degree_level") or "").lower()
                                  for b in bad)]
            targets = [c.strip().lower() for c in profile.preferences.countries if c.strip()]
            if targets:
                def _host_ok(d: dict) -> bool:
                    host = str(d.get("host_country") or "").strip().lower()
                    return not host or any(t in host or host in t for t in targets)
                out = [d for d in out if _host_ok(d)]
            emit("opportunity_scout", f"Proposer suggested {len(out)} named programs")
            return out[:30]
        except Exception as exc:
            emit("opportunity_scout",
                 f"Proposer failed ({type(exc).__name__}) — search only")
            return []

    # ── resolver: name → official page ───────────────────────────────────────

    async def _resolve(self, items: list[dict], bus) -> list[dict]:
        """Search each proposed/lead name for its official page (≤6 in flight)."""
        sem = asyncio.Semaphore(6)
        hits: list[dict] = []

        async def one(d: dict) -> None:
            name = str(d.get("name") or "").strip()
            if len(name) < 4:
                return
            hint = (d.get("official_domain_hint") or "").strip()
            toks = [t for t in re.findall(r"[A-Za-z]{3,}", name.lower())
                    if t not in _STOP_TOKENS]
            def _overlap(it) -> float:
                if not toks:
                    return 0.0
                hay = _norm(str(it.title) + " " + str(it.snippet) + " " + it.url)
                return sum(1 for t in toks if t in hay) / len(toks)
            async with sem:
                for doms in ([hint] if hint else [], []):
                    try:
                        pq = PortQuery(
                            text=q_text(name, d)[:250],
                            intent="university_scholarship", depth="basic",
                            max_results=5,
                            include_domains=list(doms))
                        r = await self._search_any(pq)
                    except Exception:
                        break
                    best = None; best_key = (-1.0, 0)
                    for it in r.items:
                        dom = extract_domain(it.url)
                        if hint and doms and hint not in dom:
                            continue
                        if classify_domain(dom) == TrustTier.AGGREGATOR:
                            continue
                        ov = _overlap(it)
                        if ov < 0.5:
                            continue
                        html = 1 if not it.url.lower().endswith(".pdf") else 0
                        if (ov, html) > best_key:
                            best_key = (ov, html); best = it
                    if best is not None:
                        hits.append({"url": best.url, "title": best.title,
                                     "snippet": best.snippet, "query_id": "",
                                     "intent": "proposed",
                                     "country": d.get("host_country"),
                                     "proposed_name": name})
                        return
                    if not hint:
                        break
                return

        await asyncio.gather(*[one(d) for d in items[:30]])
        emit("opportunity_scout", f"Resolver: {len(hits)}/{len(items)} proposed → official pages")
        return hits

    # ── registry channel: official scholarship directories per country ───────

    @staticmethod
    def _registry() -> dict:
        import json
        from pathlib import Path
        f = (Path(__file__).parent.parent / "domain" / "data"
             / "official_sources.json")
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _registry_queries(self, profile) -> list[SearchQuery]:
        reg = self._registry()
        countries = [c.lower() for c in
                     (profile.preferences.countries if profile else [])][:4]
        level = (profile.preferences.degree_level if profile else "") or "masters"
        fields = " ".join((profile.preferences.target_fields if profile else [])
                          or [])[:40]
        nat = (profile.nationality if profile else "") or ""
        out: list[SearchQuery] = []
        for c in countries:
            doms = reg.get(c) or reg.get("_multi", [])
            if not doms:
                continue
            out.append(SearchQuery(
                text=f"{level} {fields} scholarship {nat} students".strip(),
                intent="government_scholarship", country=c.title(),
                include_domains=doms, depth="basic", max_results=8))
        if nat and nat.lower() in ("indian", "nigerian"):
            key = "_india_out" if "ind" in nat.lower() else "_nigeria_out"
            out.append(SearchQuery(
                text=f"{level} {fields} scholarship {nat} students abroad".strip(),
                intent="government_scholarship",
                include_domains=reg.get(key, []),
                depth="basic", max_results=6))
        return out

    # ── official-TLD search channel ──────────────────────────────────────────

    def _official_channel_queries(self, profile) -> list[SearchQuery]:
        countries = (profile.preferences.countries if profile else [])[:4]
        level = (profile.preferences.degree_level if profile else "") or "masters"
        out = []
        for c in countries:
            domains = _TLD_HINTS.get(c.lower(), _TLD_FALLBACK)
            out.append(SearchQuery(
                text=f"{level} scholarships international students {c}",
                intent="government_scholarship", country=c,
                include_domains=domains, depth="basic", max_results=8))
        return out

    # ── execution ────────────────────────────────────────────────────────────

    async def _execute(self, queries: list[SearchQuery]) -> list[dict]:
        async def one(q: SearchQuery) -> list[dict]:
            pq = PortQuery(
                text=q.text, intent=q.intent, country=q.country,
                depth=q.depth, max_results=q.max_results,
                include_domains=q.include_domains,
                exclude_domains=q.exclude_domains, time_range="y",
            )
            try:
                r = await self._search_any(pq)
                return [{"url": i.url, "title": i.title, "snippet": i.snippet,
                         "query_id": q.id, "intent": q.intent,
                         "country": q.country} for i in r.items]
            except Exception as exc:
                emit("opportunity_scout", f"Query failed: {q.text[:50]} ({type(exc).__name__})")
                return []
        results = await asyncio.gather(*[one(q) for q in queries])
        return [it for sub in results for it in sub]

    async def _search_any(self, pq: PortQuery):
        """Primary search, falling back to the secondary on any failure."""
        try:
            return await self._search.search(pq)
        except Exception:
            if self._secondary is None:
                raise
            return await self._secondary.search(pq)

    # ── candidates ───────────────────────────────────────────────────────────

    def _to_candidates(self, items: list[dict]) -> list[Candidate]:
        seen: dict[str, Candidate] = {}
        for it in items:
            url = (it.get("url") or "").strip()
            if not url.startswith("http"):
                continue
            canonical = canonicalize_url(url)
            if canonical in seen:
                continue
            domain = extract_domain(url)
            tier = classify_domain(domain)
            if tier == TrustTier.AGGREGATOR:
                continue
            cand = Candidate(
                url=url, canonical_url=canonical, domain=domain,
                trust_tier=tier, source_query_id=str(it.get("query_id", "")),
                title=it.get("title") or None, snippet=it.get("snippet") or None,
                origin="search", country_hint=it.get("country"),
            )
            cand.prior_score = _prior_score(cand, it)
            seen[canonical] = cand
        return list(seen.values())


# ── helpers ───────────────────────────────────────────────────────────────────

def _depth_for(intent: Any) -> str:
    return "advanced" if intent in (
        "government_scholarship", "need_based", "field_fellowship") else "basic"


def _max_audit() -> int:
    try:
        from scholarship_finder.config import get_settings
        return get_settings().max_audit_candidates
    except Exception:
        return 18


def _prior_score(c: Candidate, item: dict) -> float:
    s = {TrustTier.OFFICIAL_INSTITUTION: 3.0, TrustTier.RECOGNIZED_FUNDER: 2.0,
         TrustTier.UNKNOWN: 1.0}.get(c.trust_tier, 1.0)
    blob = f"{c.title or ''} {c.snippet or ''} {c.url}".lower()
    if _FUNDING_WORDS.search(blob):
        s += 1.5
    if c.country_hint and c.country_hint.lower() in blob:
        s += 0.5
    if _INDEX_HINT.search(urlparse(c.url).path):
        s -= 1.5
    return round(s, 2)


def _profile_brief(p) -> str:
    edu = p.primary_education()
    return (
        f"target: {p.preferences.degree_level or 'study'} in "
        f"{', '.join(p.study_fields()[:3]) or 'any field'} | "
        f"countries: {', '.join(p.preferences.countries[:4]) or 'open'} | "
        f"nationality: {p.nationality or 'not stated'} | "
        f"funding need: {p.financial_need.level} | "
        f"level: {(edu.level if edu else '') or p.current_level or 'unknown'} | "
        f"languages: {', '.join(p.languages[:4]) or 'english'}"
    )


def _template_queries(profile) -> list[SearchQuery]:
    countries = (profile.preferences.countries if profile else [])[:4] or [""]
    level = (profile.preferences.degree_level if profile else "") or "masters"
    out = []
    for c in countries:
        out.append(SearchQuery(
            text=f"fully funded {level} scholarships {c} international students".strip(),
            intent="government_scholarship", country=c or None,
            depth="advanced", max_results=8))
        out.append(SearchQuery(
            text=f"university {level} scholarship {c} tuition waiver".strip(),
            intent="university_scholarship", country=c or None,
            depth="basic", max_results=8))
    nat = (profile.nationality if profile else "") or "international"
    out.append(SearchQuery(
        text=f"{nat} students fully funded {level} scholarship",
        intent="need_based", depth="advanced", max_results=8))
    return out
