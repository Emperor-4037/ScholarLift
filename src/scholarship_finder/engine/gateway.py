"""RunGateway — the coordination layer between the UI and the LangGraph engine.

Responsibilities (architecture §1.2, §3.1):
- `start_run()`       — validate intake, create per-run EventBus + SwarmState,
                        launch the graph coroutine in a **dedicated background
                        thread** with its own asyncio event loop.
- `subscribe_events()` — cursor-based event polling (thread-safe, no await)
- `get_snapshot()`    — current SwarmState for the "Top-5 cards" render
- `cancel()`          — graceful cancellation; emits FAILED_SAFE terminal event

## Concurrency model

The pipeline runs in a completely independent OS thread to avoid event loop conflicts:

    ┌──────────────────────────────────────────────────────────────┐
    │  Main Thread (FastAPI / Web UI)                             │
    │  (reads EventBus / SwarmState via thread-safe primitives)   │
    └─────────────────────────┬────────────────────────────────────┘
                              │ polling (no await)
    ┌─────────────────────────▼────────────────────────────────────┐
    │  RunHandle._thread  (daemon=True)                            │
    │    asyncio.new_event_loop() → run_until_complete(graph)      │
    │    EventBus.publish() → threading.Lock-guarded               │
    └──────────────────────────────────────────────────────────────┘

Phase A and Phase B deadlines are enforced inside the background thread via
``asyncio.wait_for`` on the graph coroutine.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from scholarship_finder.config import get_settings
from scholarship_finder.domain.pii import PIIVault
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


# ── Vault Registry ─────────────────────────────────────────────────────────────

class VaultRegistry:
    """Thread-safe registry for active PIIVault instances.
    
    Allows vaults to be accessed after the graph runner completes,
    enabling rehydration of [CANDIDATE_NAME] placeholders in the SOP.
    """
    def __init__(self):
        self._vaults: dict[str, PIIVault] = {}
        self._lock = threading.Lock()
    
    def register(self, vault: PIIVault) -> None:
        """Register a vault instance for later retrieval."""
        with self._lock:
            self._vaults[vault.vault_id] = vault
    
    def get(self, vault_id: str) -> PIIVault | None:
        """Retrieve a vault by ID, returning None if expired or not found."""
        with self._lock:
            vault = self._vaults.get(vault_id)
            if vault and vault.is_expired:
                del self._vaults[vault_id]
                return None
            return vault
    
    def cleanup_expired(self) -> None:
        """Remove all expired vaults from the registry."""
        with self._lock:
            expired = [vid for vid, v in self._vaults.items() if v.is_expired]
            for vid in expired:
                del self._vaults[vid]
                logger.debug("VaultRegistry: cleaned up expired vault %s", vid)


@dataclass
class RunHandle:
    """Live state for a single active run."""

    run_id: str
    bus: EventBus
    state: SwarmState
    _thread: threading.Thread | None = field(default=None, repr=False)
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished: bool = False
    cancelled: bool = False
    _state_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False
    )

    def set_state(self, new_state: SwarmState) -> None:
        with self._state_lock:
            self.state = new_state

    def get_state(self) -> SwarmState:
        with self._state_lock:
            return self.state


class RunGateway:
    """In-process adapter that drives the LangGraph engine.

    One ``RunGateway`` instance is created at application startup and shared
    across all sessions.

    All public methods that mutate ``_runs`` are protected by a
    ``threading.Lock`` — safe to call from any thread.
    """

    # Dynamic attributes set by composition.py
    intake_service: Any = None
    make_graph_runner: Any = None

    def __init__(
        self,
        graph_runner: Any | None = None,
        max_concurrent: int | None = None,
    ) -> None:
        cfg = get_settings()
        self._max_concurrent = max_concurrent or getattr(cfg, "max_concurrent_runs", 3)
        self._graph_runner = graph_runner
        self._runs: dict[str, RunHandle] = {}
        self._lock = threading.Lock()           # guards _runs dict mutations
        self.vault_registry = VaultRegistry()  # registry for PIIVault instances

    # ── Public API ────────────────────────────────────────────────────────────

    def start_run(
        self,
        initial_state: SwarmState,
        *,
        is_replay: bool = False,
        graph_runner: Any | None = None,
        bus: EventBus | None = None,
    ) -> str:
        """Start a new run in a background thread. Returns run_id immediately.

        **Synchronous** — safe to call from the Streamlit script thread without
        any ``asyncio.run`` or ``await``.

        Parameters
        ----------
        initial_state:
            Pre-populated SwarmState from the Intake Service.
        is_replay:
            True when serving the pre-recorded "Rahul" demo persona.
        graph_runner:
            Per-run graph coroutine factory.  Overrides the instance-level
            ``_graph_runner`` so concurrent runs don't clobber each other.
        bus:
            Pre-created EventBus to reuse across intake + graph phases.

        Returns
        -------
        str
            The run_id (UUID) — used for subsequent event polling and snapshots.
        """
        with self._lock:
            active = sum(1 for h in self._runs.values() if not h.finished)
            if self._max_concurrent is not None and active >= self._max_concurrent:
                raise RuntimeError(
                    f"Maximum concurrent runs ({self._max_concurrent}) reached. "
                    "Please wait for an active run to complete."
                )

        run_id = initial_state.run_meta.run_id
        effective_bus = bus if bus is not None else EventBus()
        handle = RunHandle(run_id=run_id, bus=effective_bus, state=initial_state)

        with self._lock:
            self._runs[run_id] = handle

        # Publish RUN_STARTED synchronously (thread-safe)
        effective_bus.publish_sync(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="gateway",
            severity=EventSeverity.INFO,
            kind=EventKind.RUN_STARTED,
            payload={"is_replay": is_replay, "run_id": run_id},
        ))

        # Resolve graph runner
        effective_runner = graph_runner if graph_runner is not None else self._graph_runner

        if effective_runner is not None:
            t = threading.Thread(
                target=self._run_in_thread,
                args=(handle, effective_runner, is_replay),
                name=f"sf-run-{run_id[:8]}",
                daemon=True,   # dies when main process exits
            )
            handle._thread = t
            t.start()
            logger.info(
                "RunGateway: started run %s in thread %s (replay=%s)",
                run_id, t.name, is_replay,
            )
        else:
            logger.warning(
                "RunGateway: no graph_runner configured — run %s is a stub", run_id
            )
            handle.finished = True

        return run_id

    def subscribe_events(self, run_id: str, cursor: int = 0) -> list[Event]:
        """Return all events after *cursor* for *run_id*.

        Purely synchronous — safe to call from the Streamlit script thread.
        Returns empty list if run_id is unknown.
        """
        handle = self._runs.get(run_id)
        if handle is None:
            return []
        return handle.bus.since(cursor)

    def get_snapshot(self, run_id: str) -> SwarmState | None:
        """Return the current (possibly partial) SwarmState for a run."""
        handle = self._runs.get(run_id)
        if handle is None:
            return None
        return handle.get_state()

    def cancel(self, run_id: str) -> bool:
        """Request cancellation of a running thread. Returns True if sent."""
        handle = self._runs.get(run_id)
        if handle is None or handle.finished:
            return False

        handle.cancelled = True
        # Publish terminal event synchronously
        handle.bus.publish_sync(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="gateway",
            severity=EventSeverity.WARNING,
            kind=EventKind.RUN_DEGRADED,
            payload={"reason": "user_cancelled", "outcome": RunOutcome.FAILED_SAFE.value},
        ))
        handle.finished = True
        logger.info("RunGateway: cancel requested for run %s", run_id)
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
                "phase": h.get_state().run_meta.phase.value,
                "outcome": h.get_state().run_meta.outcome.value,
            }
            for h in self._runs.values()
        ]

    # ── SSE Support (non-breaking addition) ─────────────────────────────────

    async def subscribe_events_async(self, run_id: str, cursor: int = 0):
        """Async generator for SSE streaming.

        Non-breaking addition - used by FastAPI SSE endpoints.
        Yields SSE-formatted messages as strings.
        """
        import json
        import asyncio

        handle = self._runs.get(run_id)
        if not handle:
            # Yield error message and stop
            yield f"data: {json.dumps({'type': 'error', 'message': 'Run not found'})}\n\n"
            return

        while not handle.finished:
            new_events = handle.bus.since(cursor)
            if new_events:
                cursor = new_events[-1].seq
                for event in new_events:
                    yield handle.bus.to_sse_message(event)
            await asyncio.sleep(0.5)  # Poll interval for SSE

        # Send final completion event
        yield f"data: {json.dumps({'type': 'complete', 'run_id': run_id})}\n\n"

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run_in_thread(
        self,
        handle: RunHandle,
        graph_runner: Any,
        is_replay: bool,
    ) -> None:
        """Entry point for the background runner thread.

        Creates a *fresh* asyncio event loop isolated from Streamlit's loop.
        This loop runs to completion and then exits — it is never shared with
        or interrupted by the Streamlit script runner.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                self._run_graph(handle, graph_runner=graph_runner, is_replay=is_replay)
            )
        finally:
            try:
                # Cancel any stray tasks (e.g. LangGraph internals)
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            finally:
                loop.close()
                asyncio.set_event_loop(None)

    async def _run_graph(
        self,
        handle: RunHandle,
        *,
        graph_runner: Any,
        is_replay: bool,
    ) -> None:
        """Drive the graph runner coroutine with Phase A/B deadline enforcement."""
        run_id = handle.run_id
        deadlines = handle.get_state().run_meta.deadlines

        try:
            updated_state = await asyncio.wait_for(
                graph_runner(handle.get_state(), handle.bus, is_replay=is_replay),
                timeout=deadlines.phase_a_hard_s + deadlines.phase_b_cap_s,
            )
            # LangGraph may return a dict — reconstruct SwarmState
            if isinstance(updated_state, dict):
                updated_state = SwarmState(**updated_state)
            handle.set_state(updated_state)
            logger.info("RunGateway: run %s completed normally", run_id)

        except asyncio.TimeoutError:
            logger.warning("RunGateway: run %s hit hard deadline", run_id)
            handle.bus.publish_sync(Event(
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

        except Exception as exc:
            logger.exception("RunGateway: run %s failed with %s", run_id, exc)
            handle.bus.publish_sync(Event(
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
    cfg = get_settings()
    rid = run_id or str(uuid.uuid4())
    return SwarmState(
        run_meta=RunMeta(
            run_id=rid,
            phase=RunPhase.INTAKE,
            deadlines=RunDeadlines(
                started_at=datetime.now(timezone.utc),
                phase_a_soft_s=cfg.phase_a_soft_deadline_s,
                phase_a_hard_s=cfg.phase_a_hard_deadline_s,
                phase_b_cap_s=cfg.phase_b_deadline_s,
            ),
            budgets=RunBudgets(
                fast_calls_limit=100,  # Reduced from 200 for token efficiency
                search_credits_limit=30,  # Reduced from 50 for token efficiency
            ),
        )
    )
