"""Agent 2: Opportunity Scout

Responsibilities (architecture §1.3, Agent 2):
- Use LLM (fast tier) to generate a disciplined SearchPlan: 3–5 queries
  spanning programs, scholarships, funders — discipline-agnostic, country/level targeted
- Execute searches via SearchPort (Tavily primary, secondary fallback)
- Canonicalize URLs, classify domain trust tiers, deduplicate by canonical_url
- Return up to ~20 unique Candidate objects for downstream auditing

LangGraph routing: after this node, routing predicate R1 checks if
sufficient diverse candidates exist; if not, triggers a retry round (max 1).

DEC-1: fast tier only for query planning. No reasoning tier calls here.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from scholarship_finder.domain.models import (
    Candidate,
    Event,
    EventKind,
    EventSeverity,
    LLMTier,
    OpportunityKind,
    SearchPlan,
    SearchQuery,
    SwarmState,
    TrustTier,
)
from scholarship_finder.domain.reducers import reduce_candidates, reduce_search_plans
from scholarship_finder.domain.trust import classify_domain
from scholarship_finder.domain.url import canonicalize_url, extract_domain
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.llm import LLMPort, TaskSpec
from scholarship_finder.ports.search import SearchPort

logger = logging.getLogger(__name__)

# Maximum URLs to pass forward
_MAX_CANDIDATES = 30

_QUERY_PLAN_PROMPT = """\
You are a search strategist. Given a candidate's academic profile (below), \
generate a precise JSON search plan to find graduate opportunities.

Return ONLY valid JSON (no markdown):
{{
  "queries": [
    {{
      "text": "<specific search query string>",
      "intent": "<one of: program | scholarship | funder>",
      "country": "<country name or null>",
      "depth": "<basic or advanced>"
    }},
    ...
  ]
}}

Rules:
- Generate exactly {num_queries} queries.
- Cover at least 2 distinct intents (program, scholarship, funder).
- Queries must be specific and search-engine optimized (include degree level, field, country).
- Do NOT use candidate names or personal information.
- Do NOT generate duplicate-intent, duplicate-country pairs.
- depth should be "advanced" for scholarship/funder queries (more results needed).

Candidate summary:
- Degree level sought: {degree_level}
- Fields of interest: {fields}
- Target countries: {countries}
- Funding required: {funding_required}
- Skills: {skills}
"""


class OpportunityScoutAgent:
    """Agent 2: Plans searches and scouts scholarship/program candidates."""

    def __init__(
        self,
        llm_port: LLMPort,
        search_port: SearchPort,
        secondary_search: SearchPort | None = None,
    ) -> None:
        self._llm = llm_port
        self._search = search_port
        self._secondary = secondary_search

    async def run(self, state: SwarmState, bus: EventBus) -> SwarmState:
        """Generate a search plan and scout candidates.

        Returns SwarmState with candidates and search_plans updated.
        """
        run_id = state.run_meta.run_id
        profile = state.profile
        if profile is None:
            logger.error("OpportunityScout: no profile in state — skipping")
            return state

        await bus.publish(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="opportunity_scout",
            severity=EventSeverity.INFO,
            kind=EventKind.NODE_STARTED,
            payload={"node": "opportunity_scout"},
        ))

        round_number = len(state.search_plans) + 1
        num_queries = 5 if round_number == 1 else 3  # fewer on retry

        # ── Step 1: LLM query planning ────────────────────────────────────────
        plan = await self._plan_queries(profile, run_id, round_number, num_queries)
        new_state = state.model_copy(deep=True)
        new_state.search_plans = reduce_search_plans(state.search_plans, plan)

        await bus.publish(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="opportunity_scout",
            severity=EventSeverity.INFO,
            kind=EventKind.PROGRESS,
            payload={
                "stage": "queries_planned",
                "count": len(plan.queries),
                "round": round_number,
            },
        ))

        # ── Step 2: Execute searches in parallel ──────────────────────────────
        all_candidates = await self._execute_searches(plan, run_id)

        # ── Step 3: Canonicalize + trust classify + dedupe ────────────────────
        new_candidates = _canonicalize_and_classify(all_candidates)

        # Merge with existing candidates
        new_state.candidates = reduce_candidates(state.candidates, new_candidates)

        await bus.publish(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="opportunity_scout",
            severity=EventSeverity.INFO,
            kind=EventKind.NODE_COMPLETED,
            payload={
                "node": "opportunity_scout",
                "new_candidates": len(new_candidates),
                "total_candidates": len(new_state.candidates),
                "round": round_number,
            },
        ))

        return new_state

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _plan_queries(
        self, profile: Any, run_id: str, round_number: int, num_queries: int
    ) -> SearchPlan:
        """Ask the LLM to generate a structured search plan."""
        import json
        import uuid

        prefs = profile.preferences
        fields_str = ", ".join(prefs.target_fields[:5]) if prefs.target_fields else "any field"
        countries_str = ", ".join(prefs.countries[:5]) if prefs.countries else "any country"
        skills_str = ", ".join(profile.skills[:10]) if profile.skills else "none listed"

        instruction = _QUERY_PLAN_PROMPT.format(
            num_queries=num_queries,
            degree_level=prefs.degree_level or "masters",
            fields=fields_str,
            countries=countries_str,
            funding_required=prefs.funding_required,
            skills=skills_str,
        )

        task = TaskSpec(
            tier=LLMTier.FAST,
            instruction=instruction,
            data_blocks=[],
            output_schema=None,
            max_output_tokens=1024,
        )

        result = await self._llm.complete(task)
        raw = result.content.strip()

        # Strip code fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        try:
            data = json.loads(raw)
            queries_raw = data.get("queries", [])
        except Exception:
            logger.warning("Query plan JSON parse error — using fallback queries")
            queries_raw = []

        queries: list[SearchQuery] = []
        for i, q in enumerate(queries_raw[:num_queries]):
            try:
                queries.append(SearchQuery(
                    id=str(uuid.uuid4()),
                    text=str(q.get("text", "")),
                    intent=str(q.get("intent", "program")),
                    country=q.get("country"),
                    domain_filters={"include": [], "exclude": []},
                    depth=str(q.get("depth", "basic")),
                    round_number=round_number,
                ))
            except Exception as exc:
                logger.debug("Malformed query at index %d: %s", i, exc)

        # Fallback if LLM returned nothing useful
        if not queries:
            queries = _fallback_queries(profile, round_number)

        return SearchPlan(queries=queries, round_number=round_number)

    async def _execute_searches(
        self, plan: SearchPlan, run_id: str
    ) -> list[dict[str, Any]]:
        """Execute all queries concurrently; merge results."""
        import asyncio

        tasks = [self._search_one(q) for q in plan.queries]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_items: list[dict[str, Any]] = []
        for q, result in zip(plan.queries, results):
            if isinstance(result, Exception):
                logger.warning("Search failed for query %r: %s", q.text[:60], result)
                # Try secondary if available
                if self._secondary:
                    try:
                        secondary_result = await self._search_one(q, port=self._secondary)
                        all_items.extend(secondary_result)
                    except Exception as exc2:
                        logger.warning("Secondary search also failed: %s", exc2)
            else:
                all_items.extend(result)

        return all_items

    async def _search_one(
        self, query: SearchQuery, port: SearchPort | None = None
    ) -> list[dict[str, Any]]:
        """Execute a single query and return raw result items."""
        active_port = port or self._search

        # Import here to avoid circular imports at module level
        from scholarship_finder.ports.search import SearchQuery as SearchPortQuery

        sq = SearchPortQuery(
            text=query.text,
            intent=query.intent,
            country=query.country,
            max_results=10,
            depth=query.depth,
            include_domains=query.domain_filters.get("include", []) if query.domain_filters else [],
            exclude_domains=query.domain_filters.get("exclude", []) if query.domain_filters else [],
        )
        result = await active_port.search(sq)
        return [
            {
                "url": item.url,
                "title": item.title,
                "snippet": item.snippet,
                "query_id": query.id,
                "intent": query.intent,
            }
            for item in result.items
        ]


# ── Pure deterministic helpers ────────────────────────────────────────────────

def _canonicalize_and_classify(
    raw_items: list[dict[str, Any]],
) -> list[Candidate]:
    """Deduplicate by canonical URL, assign trust tier, cap at MAX_CANDIDATES."""
    seen: dict[str, Candidate] = {}

    for item in raw_items:
        url = item.get("url", "")
        if not url or not url.startswith("http"):
            continue
        try:
            canonical = canonicalize_url(url)
            domain = extract_domain(canonical)
            trust = classify_domain(domain)
        except Exception:
            continue

        if canonical not in seen:
            seen[canonical] = Candidate(
                url=url,
                canonical_url=canonical,
                domain=domain,
                trust_tier=trust,
                kind_hint=_infer_kind(item.get("intent", "program")),
                source_query_id=item.get("query_id", ""),
            )
        else:
            # Keep higher trust tier
            existing = seen[canonical]
            if _tier_rank(trust) > _tier_rank(existing.trust_tier):
                seen[canonical] = Candidate(
                    url=existing.url,
                    canonical_url=canonical,
                    domain=domain,
                    trust_tier=trust,
                    kind_hint=existing.kind_hint,
                    source_query_id=existing.source_query_id,
                )

    candidates = list(seen.values())
    # Sort by trust tier (highest first) and cap
    candidates.sort(key=lambda c: _tier_rank(c.trust_tier), reverse=True)
    return candidates[:_MAX_CANDIDATES]


def _infer_kind(intent: str) -> OpportunityKind:
    if intent == "scholarship":
        return OpportunityKind.SCHOLARSHIP
    elif intent == "funder":
        return OpportunityKind.PROGRAM_WITH_FUNDING
    return OpportunityKind.PROGRAM


def _tier_rank(tier: TrustTier) -> int:
    return {
        TrustTier.OFFICIAL_INSTITUTION: 3,
        TrustTier.RECOGNIZED_FUNDER: 2,
        TrustTier.AGGREGATOR: 1,
        TrustTier.UNKNOWN: 0,
    }.get(tier, 0)


def _fallback_queries(profile: Any, round_number: int) -> list[SearchQuery]:
    """Minimal fallback when LLM query planning completely fails."""
    import uuid
    prefs = profile.preferences
    country = prefs.countries[0] if prefs.countries else "international"
    field = prefs.target_fields[0] if prefs.target_fields else "graduate"
    level = prefs.degree_level or "masters"

    return [
        SearchQuery(
            id=str(uuid.uuid4()),
            text=f"{level} {field} scholarship {country} funded",
            intent="scholarship",
            country=country,
            domain_filters={"include": [], "exclude": []},
            depth="advanced",
            round_number=round_number,
        ),
        SearchQuery(
            id=str(uuid.uuid4()),
            text=f"{level} program {field} {country} admissions",
            intent="program",
            country=country,
            domain_filters={"include": [], "exclude": []},
            depth="basic",
            round_number=round_number,
        ),
    ]
