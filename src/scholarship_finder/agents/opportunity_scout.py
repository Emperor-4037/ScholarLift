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
_MAX_CANDIDATES = 20  # Reduced from 30 for token efficiency

# Trusted domain lists for domain-constrained search
# Note: Removed aggregators (scholarships.com, fastweb.com, etc.) - only official domains
_TRUSTED_SCHOLARSHIP_DOMAINS = [
    ".gov",  # Government agencies
    ".edu",  # Universities and colleges
    # Removed: scholarships.com, fastweb.com, bigfuture.collegeboard.org (aggregators)
]

_TRUSTED_PROGRAM_DOMAINS = [
    ".edu",  # Universities and colleges
]

_TRUSTED_FUNDER_DOMAINS = [
    ".gov",  # Government agencies
    ".edu",  # University foundations
    "foundationcenter.org",
    "grant.gov",
    "nsf.gov",
    "nih.gov",
]

# Domains to exclude from search results (aggregators and listing sites)
_EXCLUDE_DOMAINS = [
    "scholarships.com",
    "fastweb.com",
    "bigfuture.collegeboard.org",
    "scholarshipamerica.org",
    "onlinemastersdegrees.com",
    "studyportals.com",
    "mastersportal.eu",
    "phdportal.eu",
    "findamasters.com",
    "findaphd.com",
    "opportunitydesk.org",
    "youthop.com",
    "scholarship-positions.com",
    "afterschoolafrica.com",
    "opportunitiesforafricans.com",
    "scholars.co",
    "studyabroad.com",
    "idp.com",
    "studylink.com",
    "niche.com",
    "cappex.com",
    "unigo.com",
    "chegg.com",
    "scholarshipowl.com",
    "scholarships360.org",
    "scholarshiptab.com",
]

# Feature toggle for query variants (disabled by default to control API costs)
_ENABLE_QUERY_VARIANTS = False

_QUERY_PLAN_PROMPT = """\
You are an expert search strategist specializing in funding opportunities. Given a candidate's academic profile (below), \
generate a precise JSON search plan to find graduate opportunities WITH FUNDING using query expansion techniques.

Return ONLY valid JSON (no markdown):
{{
  "queries": [
    {{
      "text": "<specific search query string, max 400 characters>",
      "intent": "<one of: program | scholarship | funder>",
      "country": "<country name or null>",
      "depth": "<basic or advanced>",
      "reasoning": "<brief explanation of why this query will find funding opportunities>"
    }},
    ...
  ]
}}

Query Construction Rules:
- Generate exactly {num_queries} queries.
- Cover at least 2 distinct intents (program, scholarship, funder).
- CRITICAL: For scholarship intent, ALWAYS include funding keywords: "fully funded", "financial aid", "stipend", "tuition waiver", "grant", "fellowship"
- For program intent, include "with funding", "scholarship", "assistantship", or "fellowship" to ensure funding availability
- For funder intent, target organizations: "foundation", "endowment", "grant", "funding organization"
- Use synonyms to increase recall (e.g., "scholarship OR grant OR fellowship")
- Include degree level, field, and country for specificity
- Use quotes for exact phrases when appropriate (e.g., "fully funded" OR "tuition waiver")
- Do NOT use candidate names or personal information.
- Do NOT generate duplicate-intent, duplicate-country pairs.
- depth should be "advanced" for scholarship/funder queries (more results needed).
- Keep each query under 400 characters for API compatibility.
- Focus on FINDING OPPORTUNITIES WITH ACTUAL FUNDING, not just general admissions.

Query Expansion Strategy:
- Mix broad and specific queries to capture both popular and niche opportunities
- Include both direct funding terms and institutional funding language
- Use OR operators for synonyms when space permits
- Consider alternative field names and broader category terms

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

        # Phase 2.2: Emit incremental progress for staircase streaming
        # This allows downstream agents to start processing before all searches complete
        if len(new_candidates) >= 5:  # Minimum threshold for streaming
            await bus.publish(Event(
                run_id=run_id,
                seq=0,
                timestamp=datetime.now(timezone.utc),
                node="opportunity_scout",
                severity=EventSeverity.INFO,
                kind=EventKind.PROGRESS,
                payload={
                    "stage": "incremental_candidates",
                    "count": len(new_state.candidates),
                    "round": round_number,
                },
            ))

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
                query_text = str(q.get("text", ""))
                # Validate query length for Tavily API (max 400 chars)
                if len(query_text) > 400:
                    logger.warning("Query %d exceeds 400 chars, truncating: %s", i, query_text[:60])
                    query_text = query_text[:400]
                
                # Log query quality metrics
                funding_keywords = ["funded", "funding", "scholarship", "grant", "fellowship", 
                                    "stipend", "tuition waiver", "financial aid", "assistantship"]
                keyword_count = sum(1 for kw in funding_keywords if kw.lower() in query_text.lower())
                reasoning = q.get("reasoning", "")
                
                logger.info(
                    "Query %d: intent=%s, country=%s, funding_keywords=%d, length=%d, reasoning=%s",
                    i, q.get("intent"), q.get("country"), keyword_count, len(query_text), reasoning[:50]
                )
                
                # Apply domain filtering for some queries (alternate between filtered and unfiltered)
                intent = str(q.get("intent", "program"))
                include_domains = _get_trusted_domains(intent) if i % 2 == 0 else []
                
                queries.append(SearchQuery(
                    id=str(uuid.uuid4()),
                    text=query_text,
                    intent=intent,
                    country=q.get("country"),
                    include_domains=include_domains,
                    exclude_domains=_EXCLUDE_DOMAINS,  # Exclude aggregators from all queries
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
        """Execute all queries concurrently; merge results.
        
        If query variants are enabled, generates and executes variants for each query
        to improve recall through diverse search strategies.
        """
        import asyncio

        # Generate query variants if enabled
        if _ENABLE_QUERY_VARIANTS:
            logger.info("Query variants enabled - generating expanded search set")
            all_queries: list[SearchQuery] = []
            for q in plan.queries:
                variants = _generate_query_variants(q)
                all_queries.extend(variants)
            logger.info("Generated %d total queries from %d base queries", len(all_queries), len(plan.queries))
        else:
            all_queries = plan.queries

        tasks = [self._search_one(q) for q in all_queries]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_items: list[dict[str, Any]] = []
        for q, result in zip(all_queries, results):
            if isinstance(result, Exception):
                logger.warning("Search failed for query %r: %s", q.text[:60], result)
                # Try secondary if available
                if self._secondary:
                    try:
                        secondary_result = await self._search_one(q, port=self._secondary)
                        all_items.extend(secondary_result)
                    except Exception as exc2:
                        logger.warning("Secondary search also failed: %s", exc2)
            elif isinstance(result, list):
                all_items.extend(result)
            else:
                logger.warning("Unexpected result type for query %r: %s", q.text[:60], type(result))

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
            max_results=15,  # Reduced from 25 for token efficiency
            depth=query.depth,
            include_domains=query.include_domains if query.include_domains else [],
            exclude_domains=query.exclude_domains if query.exclude_domains else [],
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


def _get_trusted_domains(intent: str) -> list[str]:
    """Return trusted domain list for a given intent."""
    if intent == "scholarship":
        return _TRUSTED_SCHOLARSHIP_DOMAINS
    elif intent == "program":
        return _TRUSTED_PROGRAM_DOMAINS
    elif intent == "funder":
        return _TRUSTED_FUNDER_DOMAINS
    return []


def _generate_query_variants(base_query: SearchQuery) -> list[SearchQuery]:
    """Generate query variants for improved recall and precision.
    
    Creates 2-3 variants per base query:
    1. Exact phrase match (with quotes)
    2. Synonym expansion (if space permits)
    3. Domain-constrained (if not already applied)
    
    Returns list of variants including the original.
    """
    import uuid
    import re
    
    variants = [base_query]
    base_text = base_query.text
    
    # Variant 1: Add quotes for exact phrase matching (if not already quoted)
    if '"' not in base_text and len(base_text) < 350:
        # Identify key phrases to quote (2-3 word sequences)
        words = base_text.split()
        if len(words) >= 3:
            # Quote the first 2-3 meaningful words
            quote_phrase = ' '.join(words[:3])
            quoted_text = base_text.replace(quote_phrase, f'"{quote_phrase}"', 1)
            if quoted_text != base_text and len(quoted_text) <= 400:
                variants.append(SearchQuery(
                    id=str(uuid.uuid4()),
                    text=quoted_text,
                    intent=base_query.intent,
                    country=base_query.country,
                    include_domains=base_query.include_domains,
                    exclude_domains=base_query.exclude_domains,
                    depth=base_query.depth,
                    round_number=base_query.round_number,
                ))
    
    # Variant 2: Add domain constraint if not already present
    if not base_query.include_domains:
        trusted_domains = _get_trusted_domains(base_query.intent)
        if trusted_domains:
            # Apply only a subset of domains to avoid over-restriction
            subset_domains = trusted_domains[:3]  # Top 3 most trusted
            variants.append(SearchQuery(
                id=str(uuid.uuid4()),
                text=base_text,
                intent=base_query.intent,
                country=base_query.country,
                include_domains=subset_domains,
                exclude_domains=base_query.exclude_domains,
                depth=base_query.depth,
                round_number=base_query.round_number,
            ))
    
    return variants


def _fallback_queries(profile: Any, round_number: int) -> list[SearchQuery]:
    """Enhanced fallback with query expansion when LLM query planning fails.
    
    Uses multiple funding keyword variants and synonyms to improve recall.
    """
    import uuid
    prefs = profile.preferences
    country = prefs.countries[0] if prefs.countries else "international"
    field = prefs.target_fields[0] if prefs.target_fields else "graduate"
    level = prefs.degree_level or "masters"

    # Query expansion: multiple keyword variants for each intent
    return [
        # Variant 1: Direct funding terms with exact phrase matching
        SearchQuery(
            id=str(uuid.uuid4()),
            text=f'"fully funded" {level} {field} scholarship {country} stipend',
            intent="scholarship",
            country=country,
            include_domains=[],
            exclude_domains=_EXCLUDE_DOMAINS,
            depth="advanced",
            round_number=round_number,
        ),
        # Variant 2: Synonym expansion for financial aid
        SearchQuery(
            id=str(uuid.uuid4()),
            text=f'{level} {field} scholarship {country} (financial aid OR "tuition waiver" OR grant)',
            intent="scholarship",
            country=country,
            include_domains=[],
            exclude_domains=_EXCLUDE_DOMAINS,
            depth="advanced",
            round_number=round_number,
        ),
        # Variant 3: Program funding with institutional language
        SearchQuery(
            id=str(uuid.uuid4()),
            text=f'{level} program {field} {country} "with funding" OR assistantship OR fellowship',
            intent="program",
            country=country,
            include_domains=[],
            exclude_domains=_EXCLUDE_DOMAINS,
            depth="advanced",
            round_number=round_number,
        ),
        # Variant 4: International-specific funding opportunities
        SearchQuery(
            id=str(uuid.uuid4()),
            text=f'"fully funded" {level} scholarship {country} international students OR global',
            intent="scholarship",
            country=country,
            include_domains=[],
            exclude_domains=_EXCLUDE_DOMAINS,
            depth="advanced",
            round_number=round_number,
        ),
        # Variant 5: Research-focused funding and fellowships
        SearchQuery(
            id=str(uuid.uuid4()),
            text=f'{field} graduate fellowship {country} "research funding" OR grant OR endowment',
            intent="funder",
            country=country,
            include_domains=[],
            exclude_domains=_EXCLUDE_DOMAINS,
            depth="advanced",
            round_number=round_number,
        ),
    ]
