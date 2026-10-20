"""RunGateway — the coordination layer between the UI and the LangGraph engine.

Responsibilities (architecture §1.2, §3.1):
- `start_run()`       — validate intake, create per-run EventBus + SwarmState,
                        launch the graph coroutine as a background asyncio Task.
- `subscribe_events()` — cursor-based event polling (SSE or polling UI)
- `get_snapshot()`    — current SwarmState for the "Top-5 cards" render
- `cancel()`          — graceful cancellation; emits FAILED_SAFE terminal event

Concurrency model (LITE profile):
- Single event loop (Streamlit runs asyncio via `asyncio.run` per script re-run)
- Runs are stored in an in-process dict keyed by run_id
- MAX_CONCURRENT_RUNS enforced (default 3 from Settings)

Phase A and Phase B deadlines are enforced here via asyncio.wait_for on the
graph coroutine; the graph itself implements soft-cutoff cooperative yields.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from scholarship_finder.config import get_settings
from scholarship_finder.domain.models import (
    Event,
    EventKind,
    EventSeverity,
    RunMeta,
    RunOutcome,
    RunPhase,
    RunDeadlines,
    RunBudgets,
    SwarmState,
)
from scholarship_finder.engine.event_bus import EventBus

logger = logging.getLogger(__name__)


@dataclass
class RunHandle:
    """Live state for a single active run."""

    run_id: str
    bus: EventBus
    state: SwarmState
    task: asyncio.Task[None] | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished: bool = False
    cancelled: bool = False


class RunGateway:
    """In-process adapter (LITE profile) that drives the LangGraph engine.

    One ``RunGateway`` instance is created at application startup and shared
    across all Streamlit sessions.
    """

    def __init__(
        self,
        graph_runner: Any | None = None,  # injected by composition root
        max_concurrent: int | None = None,
    ) -> None:
        cfg = get_settings()
        self._max_concurrent = max_concurrent or getattr(cfg, "max_concurrent_runs", 3)
        self._graph_runner = graph_runner  # callable(SwarmState, EventBus) -> Awaitable[SwarmState]
        self._runs: dict[str, RunHandle] = {}
        self._lock = asyncio.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    async def start_run(
        self,
        initial_state: SwarmState,
        *,
        is_replay: bool = False,
        graph_runner: Any | None = None,
        bus: EventBus | None = None,
    ) -> str:
        """Start a new run and return its run_id.

        Parameters
        ----------
        initial_state:
            Pre-populated SwarmState from the Intake Service (profile already
            set, PII already vaulted, run_meta initialized).
        is_replay:
            True when serving the recorded "Rahul" demo persona — the graph
            runner receives this flag and loads fixtures instead of live calls.
        graph_runner:
            Per-run graph coroutine factory. Overrides the instance-level
            ``_graph_runner`` so concurrent runs don't clobber each other.
        bus:
            Pre-created EventBus to reuse (avoids creating a second bus that
            would be invisible to the graph runner).

        Returns
        -------
        str
            The run_id (UUID) — used for subsequent event polling and snapshots.

        Raises
        ------
        RuntimeError
            If MAX_CONCURRENT_RUNS is already reached.
        """
        async with self._lock:
            active = sum(1 for h in self._runs.values() if not h.finished)
            if active >= self._max_concurrent:
                raise RuntimeError(
                    f"Maximum concurrent runs ({self._max_concurrent}) reached. "
                    "Please wait for an active run to complete."
                )

        run_id = initial_state.run_meta.run_id
        # Reuse caller-supplied bus so graph events reach the polling loop
        effective_bus = bus if bus is not None else EventBus()
        handle = RunHandle(run_id=run_id, bus=effective_bus, state=initial_state)

        async with self._lock:
            self._runs[run_id] = handle

        # Emit RUN_STARTED immediately
        await effective_bus.publish(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="gateway",
            severity=EventSeverity.INFO,
            kind=EventKind.RUN_STARTED,
            payload={"is_replay": is_replay, "run_id": run_id},
        ))

        # Resolve graph runner: per-run arg takes priority, then instance default
        effective_runner = graph_runner if graph_runner is not None else self._graph_runner

        # Launch graph as background task
        if effective_runner is not None:
            task = asyncio.create_task(
                self._run_graph(handle, is_replay=is_replay, graph_runner=effective_runner),
                name=f"run:{run_id}",
            )
            handle.task = task
        else:
            logger.warning("RunGateway: no graph_runner configured — run %s is a stub", run_id)

        logger.info("RunGateway: started run %s (replay=%s)", run_id, is_replay)
        return run_id

    def subscribe_events(self, run_id: str, cursor: int = 0) -> list[Event]:
        """Return all events after *cursor* for *run_id*.

        Suitable for polling (Streamlit re-runs) and SSE (FastAPI FULL profile).
        Returns empty list if run_id is unknown.
        """
        handle = self._runs.get(run_id)
        if handle is None:
            return []
        return handle.bus.since(cursor)

    async def wait_for_events(
        self, run_id: str, cursor: int = 0, timeout_s: float = 30.0
    ) -> list[Event]:
        """Long-poll variant: block until new events arrive or timeout."""
        handle = self._runs.get(run_id)
        if handle is None:
            return []
        return await handle.bus.wait_for_new(cursor, timeout_s=timeout_s)

    def get_snapshot(self, run_id: str) -> SwarmState | None:
        """Return the current (possibly partial) SwarmState for a run."""
        handle = self._runs.get(run_id)
        return handle.state if handle else None

    async def cancel(self, run_id: str) -> bool:
        """Cancel a running task. Returns True if cancellation was sent."""
        handle = self._runs.get(run_id)
        if handle is None or handle.finished:
            return False

        handle.cancelled = True
        if handle.task and not handle.task.done():
            handle.task.cancel()
            logger.info("RunGateway: cancel requested for run %s", run_id)

        # Emit terminal event
        await handle.bus.publish(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="gateway",
            severity=EventSeverity.WARNING,
            kind=EventKind.RUN_DEGRADED,
            payload={"reason": "user_cancelled", "outcome": RunOutcome.FAILED_SAFE.value},
        ))
        handle.finished = True
        return True

    def list_runs(self) -> list[dict]:
        """Summary of all runs — for the pre-flight canary / admin UI."""
        return [
            {
                "run_id": h.run_id,
                "started_at": h.started_at.isoformat(),
                "finished": h.finished,
                "cancelled": h.cancelled,
                "event_count": h.bus.event_count(),
                "phase": h.state.run_meta.phase.value,
                "outcome": h.state.run_meta.outcome.value,
            }
            for h in self._runs.values()
        ]

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _run_graph(self, handle: RunHandle, *, is_replay: bool, graph_runner: Any | None = None) -> None:
        """Drive the graph runner coroutine with Phase A/B deadline enforcement."""
        run_id = handle.run_id
        deadlines = handle.state.run_meta.deadlines
        runner = graph_runner if graph_runner is not None else self._graph_runner

        try:
            updated_state = await asyncio.wait_for(
                runner(handle.state, handle.bus, is_replay=is_replay),
                timeout=deadlines.phase_a_hard_s + deadlines.phase_b_cap_s,
            )
            # Update state in place
            handle.state = updated_state
            logger.info("RunGateway: run %s completed normally", run_id)

        except asyncio.TimeoutError:
            logger.warning("RunGateway: run %s hit hard deadline", run_id)
            await handle.bus.publish(Event(
                run_id=run_id,
                seq=0,
                timestamp=datetime.now(timezone.utc),
                node="gateway",
                severity=EventSeverity.WARNING,
                kind=EventKind.RUN_DEGRADED,
                payload={
                    "reason": "hard_deadline_exceeded",
                    "outcome": RunOutcome.DEGRADED.value,
                },
            ))

        except asyncio.CancelledError:
            logger.info("RunGateway: run %s cancelled", run_id)
            # cancel() already published the terminal event

        except Exception as exc:
            logger.exception("RunGateway: run %s failed with %s", run_id, exc)
            await handle.bus.publish(Event(
                run_id=run_id,
                seq=0,
                timestamp=datetime.now(timezone.utc),
                node="gateway",
                severity=EventSeverity.ERROR,
                kind=EventKind.RUN_DEGRADED,
                payload={
                    "reason": str(exc),
                    "outcome": RunOutcome.FAILED_SAFE.value,
                },
            ))

        finally:
            handle.finished = True


# ── Factory helper ────────────────────────────────────────────────────────────

def make_initial_state(run_id: str | None = None) -> SwarmState:
    """Create a blank SwarmState for a new run with defaults."""
    rid = run_id or str(uuid.uuid4())
    return SwarmState(
        run_meta=RunMeta(
            run_id=rid,
            phase=RunPhase.INTAKE,
            deadlines=RunDeadlines(started_at=datetime.now(timezone.utc)),
            budgets=RunBudgets(
                fast_calls_remaining=200,
                search_credits_remaining=50,
            ),
        )
    )
