"""LangGraph Run Engine — wires the 5-agent pipeline into a StateGraph.

Graph topology (architecture §1.2):
    INTAKE → A1 (ProfileEnricher) → A2 (OpportunityScout)
           → R1 (routing: enough candidates?) → [retry A2 | A3 (SourceAuditor)]
           → A3 → R2 (routing: Phase A deadline?) → A4 (RankingStrategist)
           → A4 → PHASE_A_READY event → R3 (routing: SOP needed?) → A5 (ApplicationStrategist)
           → A5 → END

Routing predicates R1, R2, R3 are pure deterministic Python functions — no LLM.

Phase A hard stop enforced via asyncio timeout in RunGateway.
Phase A PHASE_A_READY event emitted after A4 completes (top-5 ready).
Phase B (A5) runs after PHASE_A_READY, within its own 120s budget.

Dependency injection: all adapters passed via GraphConfig — no global singletons.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from langgraph.graph import END, StateGraph

from scholarship_finder.domain.models import (
    Event,
    EventKind,
    EventSeverity,
    RunOutcome,
    RunPhase,
    SwarmState,
)
from scholarship_finder.engine.event_bus import EventBus

logger = logging.getLogger(__name__)

# Minimum candidates before skipping the retry round
_MIN_CANDIDATES_BEFORE_RETRY = 5
# Minimum verified opportunities to proceed to ranking
_MIN_OPPORTUNITIES = 1


# ── Routing predicates (pure code — no LLM) ───────────────────────────────────

def _r1_enough_candidates(state: SwarmState) -> str:
    """R1: Do we have enough diverse candidates to proceed?"""
    n = len(state.candidates)
    retry_count = state.run_meta.retry_count
    if n >= _MIN_CANDIDATES_BEFORE_RETRY or retry_count >= state.run_meta.max_retries:
        logger.debug("R1: %d candidates, proceeding to source_auditor", n)
        return "source_auditor"
    logger.info("R1: only %d candidates, triggering retry round", n)
    return "retry_scout"


def _r2_has_opportunities(state: SwarmState) -> str:
    """R2: Did the auditor find enough verified opportunities?"""
    n = len(state.opportunities)
    if n >= _MIN_OPPORTUNITIES:
        return "ranking_strategist"
    logger.warning("R2: %d opportunities — insufficient; marking DEGRADED", n)
    return "degraded_end"


def _r3_sop_needed(state: SwarmState) -> str:
    """R3: Should we generate a SOP (Phase B)?"""
    if state.top_5 and state.profile:
        return "application_strategist"
    return "complete_end"


# ── Node wrappers ─────────────────────────────────────────────────────────────

def make_graph(
    *,
    profile_enricher_agent,
    opportunity_scout_agent,
    source_auditor_agent,
    ranking_strategist_agent,
    application_strategist_agent,
    bus: EventBus,
    scrubbed_text: str,
    github_username: str | None = None,
    vault=None,
    is_replay: bool = False,
) -> Callable[[SwarmState], Any]:
    """Build and compile the LangGraph StateGraph. Returns a runnable coroutine factory."""

    async def node_profile_enricher(state: SwarmState) -> SwarmState:
        state = _advance_phase(state, RunPhase.PHASE_A)
        return await profile_enricher_agent.run(
            state, bus,
            scrubbed_text=scrubbed_text,
            github_username=github_username,
        )

    async def node_opportunity_scout(state: SwarmState) -> SwarmState:
        return await opportunity_scout_agent.run(state, bus)

    async def node_retry_scout(state: SwarmState) -> SwarmState:
        """Increment retry counter and re-run the scout."""
        new_meta = state.run_meta.model_copy(
            update={"retry_count": state.run_meta.retry_count + 1}
        )
        state = state.model_copy(update={"run_meta": new_meta})
        await bus.publish(Event(
            run_id=state.run_meta.run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="retry_scout",
            severity=EventSeverity.WARNING,
            kind=EventKind.RETRY_TRIGGERED,
            payload={"retry_count": state.run_meta.retry_count},
        ))
        return await opportunity_scout_agent.run(state, bus)

    async def node_source_auditor(state: SwarmState) -> SwarmState:
        return await source_auditor_agent.run(state, bus)

    async def node_ranking_strategist(state: SwarmState) -> SwarmState:
        updated = await ranking_strategist_agent.run(state, bus)
        # Emit PHASE_A_READY after ranking completes
        await bus.publish(Event(
            run_id=updated.run_meta.run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="ranking_strategist",
            severity=EventSeverity.INFO,
            kind=EventKind.PHASE_A_READY,
            payload={
                "top_5": updated.top_5,
                "opportunity_count": len(updated.opportunities),
            },
        ))
        return updated

    async def node_application_strategist(state: SwarmState) -> SwarmState:
        return await application_strategist_agent.run(state, bus, vault=vault)

    async def node_degraded_end(state: SwarmState) -> SwarmState:
        """Emit terminal degraded event and mark state."""
        await bus.publish(Event(
            run_id=state.run_meta.run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="graph",
            severity=EventSeverity.WARNING,
            kind=EventKind.RUN_DEGRADED,
            payload={"reason": "insufficient_opportunities", "outcome": RunOutcome.PARTIAL.value},
        ))
        new_meta = state.run_meta.model_copy(update={"outcome": RunOutcome.PARTIAL})
        return state.model_copy(update={"run_meta": new_meta})

    async def node_complete_end(state: SwarmState) -> SwarmState:
        """Emit RUN_COMPLETED terminal event."""
        outcome = RunOutcome.COMPLETE if state.application_package else RunOutcome.PARTIAL
        await bus.publish(Event(
            run_id=state.run_meta.run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="graph",
            severity=EventSeverity.INFO,
            kind=EventKind.RUN_COMPLETED,
            payload={
                "outcome": outcome.value,
                "top_5": state.top_5,
                "has_sop": state.application_package is not None,
            },
        ))
        new_meta = state.run_meta.model_copy(
            update={"outcome": outcome, "phase": RunPhase.COMPLETED}
        )
        return state.model_copy(update={"run_meta": new_meta})

    # ── Build StateGraph ──────────────────────────────────────────────────────
    builder = StateGraph(SwarmState)

    builder.add_node("profile_enricher", node_profile_enricher)
    builder.add_node("opportunity_scout", node_opportunity_scout)
    builder.add_node("retry_scout", node_retry_scout)
    builder.add_node("source_auditor", node_source_auditor)
    builder.add_node("ranking_strategist", node_ranking_strategist)
    builder.add_node("application_strategist", node_application_strategist)
    builder.add_node("degraded_end", node_degraded_end)
    builder.add_node("complete_end", node_complete_end)

    # Entry point
    builder.set_entry_point("profile_enricher")

    # Linear edges
    builder.add_edge("profile_enricher", "opportunity_scout")

    # R1: enough candidates?
    builder.add_conditional_edges(
        "opportunity_scout",
        _r1_enough_candidates,
        {"source_auditor": "source_auditor", "retry_scout": "retry_scout"},
    )
    builder.add_edge("retry_scout", "source_auditor")

    # R2: has opportunities?
    builder.add_conditional_edges(
        "source_auditor",
        _r2_has_opportunities,
        {"ranking_strategist": "ranking_strategist", "degraded_end": "degraded_end"},
    )

    # R3: SOP needed?
    builder.add_conditional_edges(
        "ranking_strategist",
        _r3_sop_needed,
        {"application_strategist": "application_strategist", "complete_end": "complete_end"},
    )

    builder.add_edge("application_strategist", "complete_end")
    builder.add_edge("degraded_end", END)
    builder.add_edge("complete_end", END)

    graph = builder.compile()

    async def run_graph(initial_state: SwarmState, event_bus: EventBus, **kwargs) -> SwarmState:
        """Execute the compiled graph from initial_state."""
        result = await graph.ainvoke(initial_state)
        return result

    return run_graph


def _advance_phase(state: SwarmState, phase: RunPhase) -> SwarmState:
    """Move run_meta.phase forward (monotonic)."""
    new_meta = state.run_meta.model_copy(update={"phase": phase})
    return state.model_copy(update={"run_meta": new_meta})
