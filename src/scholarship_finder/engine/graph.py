"""LangGraph Run Engine — wires the agent pipeline into a StateGraph.

Topology (plan §3 Step D):

    profile_enricher → opportunity_scout → source_auditor ─R─┐
                         ▲ (lead expansion, max 1 round) ◀───┘
                       ranking_strategist ──┬─ gap_advisor ──┤
                                           └─ application_strategist ─▶ finalize

Nodes return **partial dict updates**; a node's full-state return value
(legacy agents) is normalised to an update automatically. Routing
predicates are pure functions of state. Deadlines/cancellation are
enforced by the RunGateway around the stream, plus ``RunContext`` checks.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Callable

from langgraph.graph import END, StateGraph

from scholarship_finder.domain.models import (
    EventKind,
    EventSeverity,
    RunOutcome,
    RunPhase,
    SwarmState,
)
from scholarship_finder.engine import context as run_context
from scholarship_finder.engine.event_bus import EventBus

logger = logging.getLogger(__name__)


# ── Node-result normalisation ────────────────────────────────────────────────

def _to_update(result: Any) -> dict[str, Any]:
    """Normalise an agent's return value to a partial state update dict."""
    if result is None:
        return {}
    if isinstance(result, dict):
        return result
    if isinstance(result, SwarmState):
        return result.model_dump()
    return {}


def _advance_phase(update: dict[str, Any], state: SwarmState, phase: RunPhase) -> dict[str, Any]:
    """Overlay a monotonic phase advance onto a node update."""
    meta = state.run_meta.model_copy(update={"phase": phase})
    update["run_meta"] = meta
    return update


# ── Routing predicates (pure code — no LLM) ───────────────────────────────────

def _expand_blocked(state) -> bool:
    """Expansion only while elapsed <150 s and ≥60 s of discovery budget left."""
    try:
        from scholarship_finder.engine import context as rc
        ctx = rc.current()
        if ctx is None:
            return False
        return ctx.elapsed() >= 150 or ctx.discovery_time_left() <= 60
    except Exception:
        return False


def _after_auditor(state: "SwarmState | dict", target: int) -> str:
    """After auditing: expand leads once, rank, or go straight to advice."""
    if isinstance(state, dict):
        state = SwarmState.model_validate(state)
    verified = state.existence_verified_count()
    if (
        verified < target
        and state.run_meta.expansion_rounds == 0
        and (state.leads or state.candidates)
        and run_context.time_left() > 75
    ):
        return ["lead_expansion"]
    if verified >= 1:
        return ["ranking_strategist"]
    # Both nodes must fire so the finalize join (multi-source edge) completes;
    # application_strategist no-ops when there is no top-5.
    return ["gap_advisor", "application_strategist"]



# ── Graph factory ─────────────────────────────────────────────────────────────

def make_graph(
    *,
    profile_enricher_agent: Any = None,
    opportunity_scout_agent: Any = None,
    source_auditor_agent: Any = None,
    ranking_strategist_agent: Any = None,
    gap_advisor_agent: Any = None,
    application_strategist_agent: Any = None,
    bus: EventBus | None = None,
    scrubbed_text: str | None = None,
    github_username: str | None = None,
    vault: Any = None,
    is_replay: bool = False,
    target_verified: int = 10,
    **_ignored: Any,
) -> Callable[[SwarmState, EventBus], AsyncIterator[SwarmState]]:
    """Build and compile the StateGraph. Returns a per-run stream factory."""

    def _wrap(name: str, agent: Any, call: Callable | None = None,
              phase: RunPhase | None = None):
        """Wrap an agent call into a LangGraph node returning a dict update."""
        async def node(state: SwarmState) -> dict[str, Any]:
            run_context.emit(name, f"{name} started", kind=EventKind.NODE_STARTED)
            try:
                if agent is None or call is None:
                    result = None
                else:
                    result = await call(state)
            except Exception as exc:
                logger.exception("Graph: node %s failed — run continues degraded", name)
                run_context.emit(
                    name, f"{name} failed: {exc}",
                    kind=EventKind.WARNING, severity=EventSeverity.WARNING,
                )
                result = None
            update = _to_update(result)
            if phase is not None:
                update = _advance_phase(update, state, phase)
            run_context.emit(name, f"{name} finished", kind=EventKind.NODE_COMPLETED)
            return update
        return node

    async def node_lead_expansion(state: SwarmState) -> dict[str, Any]:
        """One expansion round over auditor-found leads (marks expansion_rounds)."""
        update: dict[str, Any] = {}
        expand = getattr(opportunity_scout_agent, "expand", None)
        if expand is not None:
            try:
                update = _to_update(await expand(state))
            except Exception as exc:
                logger.exception("Graph: lead_expansion failed")
                run_context.emit(
                    "lead_expansion", f"Lead expansion failed: {exc}",
                    kind=EventKind.WARNING, severity=EventSeverity.WARNING,
                )
        meta = state.run_meta.model_copy(update={"expansion_rounds": 1})
        update["run_meta"] = meta
        return update

    async def node_finalize(state: SwarmState) -> dict[str, Any]:
        """Terminal node: outcome COMPLETE when top-5 + SOP, else DEGRADED."""
        pkg = state.applications.get(state.top_5[0]) if state.top_5 else None
        sop_ready = pkg is not None and pkg.status == "ready"
        outcome = RunOutcome.COMPLETE if (state.top_5 and sop_ready) else RunOutcome.DEGRADED
        meta = state.run_meta.model_copy(
            update={"outcome": outcome, "phase": RunPhase.COMPLETED}
        )
        run_context.emit(
            "finalize",
            "Run completed" if outcome == RunOutcome.COMPLETE
            else "Run finished with partial results",
            kind=(EventKind.RUN_COMPLETED if outcome == RunOutcome.COMPLETE
                  else EventKind.RUN_DEGRADED),
            severity=(EventSeverity.INFO if outcome == RunOutcome.COMPLETE
                      else EventSeverity.WARNING),
            outcome=outcome.value,
            top_5=list(state.top_5),
        )
        try:
            from scholarship_finder.engine import context as _rc
            c = _rc.current()
            if c is not None:
                run_context.emit("llm_router",
                                 f"LLM usage: {c.llm_usage_summary()}")
        except Exception:
            pass
        return {"run_meta": meta}

    builder = StateGraph(SwarmState)

    builder.add_node("profile_enricher", _wrap(
        "profile_enricher", profile_enricher_agent,
        (lambda s: profile_enricher_agent.run(
            s, bus, scrubbed_text=scrubbed_text or "",
            github_username=github_username,
        )) if profile_enricher_agent is not None else None,
        phase=RunPhase.PHASE_A,
    ))
    builder.add_node("opportunity_scout", _wrap(
        "opportunity_scout", opportunity_scout_agent,
        (lambda s: opportunity_scout_agent.run(
            s, bus, scrubbed_text=scrubbed_text,
        )) if opportunity_scout_agent is not None else None,
    ))
    builder.add_node("source_auditor", _wrap(
        "source_auditor", source_auditor_agent,
        (lambda s: source_auditor_agent.run(s, bus))
        if source_auditor_agent is not None else None,
    ))
    builder.add_node("lead_expansion", node_lead_expansion)
    builder.add_node("ranking_strategist", _wrap(
        "ranking_strategist", ranking_strategist_agent,
        (lambda s: ranking_strategist_agent.run(s, bus))
        if ranking_strategist_agent is not None else None,
        phase=RunPhase.PHASE_B,
    ))
    builder.add_node("gap_advisor", _wrap(
        "gap_advisor", gap_advisor_agent,
        (lambda s: gap_advisor_agent.run(s, bus))
        if gap_advisor_agent is not None else None,
    ))
    async def node_application_strategist(state: SwarmState) -> dict[str, Any]:
        """SOP branch — a no-op when no top-5 exists (the finalize join
        still needs this node to fire)."""
        if not state.top_5:
            return {}
        return await _wrap(
            "application_strategist", application_strategist_agent,
            (lambda s: application_strategist_agent.run(s, bus, vault=vault))
            if application_strategist_agent is not None else None,
        )(state)

    builder.add_node("application_strategist", node_application_strategist)
    builder.add_node("finalize", node_finalize)

    builder.set_entry_point("profile_enricher")
    builder.add_edge("profile_enricher", "opportunity_scout")
    builder.add_edge("opportunity_scout", "source_auditor")
    builder.add_conditional_edges(
        "source_auditor",
        lambda state: _after_auditor(state, target_verified),
        ["lead_expansion", "ranking_strategist", "gap_advisor", "application_strategist"],
    )
    builder.add_edge("lead_expansion", "source_auditor")
    builder.add_edge("ranking_strategist", "gap_advisor")
    builder.add_edge("ranking_strategist", "application_strategist")
    builder.add_edge(["gap_advisor", "application_strategist"], "finalize")
    builder.add_edge("finalize", END)

    graph = builder.compile()

    async def run_graph(initial_state: SwarmState, event_bus: EventBus, **_kw: Any) -> AsyncIterator[SwarmState]:
        """Stream SwarmState values after every super-step (stream_mode='values')."""
        config = {"configurable": {"max_concurrency": 6}}
        async for values in graph.astream(initial_state, config=config, stream_mode="values"):
            yield SwarmState.model_validate(values)

    return run_graph
