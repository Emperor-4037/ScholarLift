"""Agent 6: Gap Advisor — clusters closable gaps into an ImprovementPlan.

Clustering, unlock lists, targets and priorities are deterministic
(domain.gaps + enrichment_tips). One FAST complete_json writes title/action
text per cluster (≤7); fallback = templates.
"""
from __future__ import annotations

import logging

from scholarship_finder.domain import gaps as gap_domain
from scholarship_finder.domain.models import (
    ImprovementPlan,
    LLMTier,
    Suggestion,
    SwarmState,
)
from scholarship_finder.engine.context import emit
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.llm import LLMPort, TaskSpec

logger = logging.getLogger(__name__)

_PROMPT = """For each gap cluster below, write a JSON array entry:
{{"cluster": <i>, "title": "short action title", "action": "1-sentence concrete step"}}.
Never invent numbers or opportunity names — they are fixed by us.

Clusters:
{clusters}"""

_TITLES = {
    "test": "Improve or take a language test",
    "academic": "Strengthen academic evidence",
    "experience": "Gain relevant experience",
    "research": "Build a research profile",
    "documents": "Prepare required documents",
}


class GapAdvisorAgent:
    def __init__(self, llm_port: LLMPort) -> None:
        self._llm = llm_port

    async def run(self, state: SwarmState, bus: EventBus) -> dict:
        clusters = gap_domain.aggregate_gaps(
            state.assessments, state.opportunities, state.profile)[:7]
        seeds = [c.to_seed() for c in clusters]
        if not seeds:
            plan = ImprovementPlan(
                summary="No closable gaps found — profile looks strong.",
                near_miss_ids=[a.opportunity_id for a in state.assessments
                               if a.status == "near_miss"],
                generated_by="fallback")
            emit("gap_advisor", "No closable gaps found")
            return {"improvement_plan": plan}

        texts = await self._write(seeds)
        suggestions = []
        for i, seed in enumerate(seeds):
            t = texts.get(i, {})
            suggestions.append(Suggestion(
                title=t.get("title") or
                      _TITLES.get(seed["category"], "Close this gap"),
                action=t.get("action") or f"Address {seed['rule_name']}",
                rationale=seed.get("rationale", ""),
                category=seed["category"],
                priority=int(seed.get("priority") or 3),
                unlocks=seed.get("unlocks") or [],
                unlock_names=seed.get("unlock_names") or [],
                deadline_hint=seed.get("deadline_hint") or "",
            ))
        n_near = len([a for a in state.assessments if a.status == "near_miss"])
        plan = ImprovementPlan(
            summary=f"{len(suggestions)} suggestions; {n_near} near-miss "
                    f"opportunities could unlock with these fixes.",
            suggestions=suggestions,
            near_miss_ids=[a.opportunity_id for a in state.assessments
                           if a.status == "near_miss"],
        )
        emit("gap_advisor", f"Improvement plan: {len(suggestions)} suggestions "
                            f"({n_near} near-miss opportunities)")
        return {"improvement_plan": plan}

    async def _write(self, seeds: list[dict]) -> dict[int, dict]:
        try:
            block = "\n".join(
                f"{i}: category={s['category']} rule={s['rule_name']} "
                f"target={s.get('target','')} gap={s.get('gap')} "
                f"unlocks={s.get('unlock_names', [])}"
                for i, s in enumerate(seeds))
            res = await self._llm.complete_json(TaskSpec(
                json_schema={"type": "array", "items": {"type": "object"}},
                tier=LLMTier.FAST, system=_PROMPT.format(clusters=block),
                max_output_tokens=1200, purpose="gap_advice"))
            parsed = res.parsed
            if isinstance(parsed, dict):
                parsed = next((v for v in parsed.values() if isinstance(v, list)), [])
            return {int(d["cluster"]): d for d in (parsed or [])
                    if isinstance(d, dict) and "cluster" in d}
        except Exception as exc:
            emit("gap_advisor", f"LLM suggestion text failed ({type(exc).__name__}) — templates")
            return {}
