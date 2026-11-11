"""RunGateway — the coordination layer between the API and the LangGraph engine.

Responsibilities:
- ``start_run()``       — create per-run EventBus + RunContext, launch the
                          graph in a dedicated background thread (own loop).
- ``subscribe_events*`` — cursor-based event polling + SSE async generator.
- ``get_snapshot()``    — latest SwarmState (updated on every graph step).
- ``cancel()``          — real cancellation: cancel_event + task.cancel().
- ``request_sop()``     — on-demand SOP draft for one opportunity.
- ``purge()``           — delete run state + its PII vault.

Concurrency model: each run gets its own OS thread + event loop. The
RunContext (deadlines, budgets, cancel_event, usage) travels in a ContextVar
so agents/adapters need no extra parameters. ``handle.set_state()`` is called
on *every* streamed graph step, so timeouts never lose finished work.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from scholarship_finder.config import get_settings
from scholarship_finder.domain.models import (
    ApplicationPackage,
    Event,
    EventKind,
    EventSeverity,
    RunDeadlines,
    RunMeta,
    RunOutcome,
    RunPhase,
    SwarmState,
)
from scholarship_finder.domain.pii import PIIVault
from scholarship_finder.engine.context import RunContext, set_context, reset_context
from scholarship_finder.engine.event_bus import EventBus

logger = logging.getLogger(__name__)


# ── Vault Registry ─────────────────────────────────────────────────────────────

class VaultRegistry:
    """Thread-safe registry for active PIIVault instances."""

    def __init__(self):
        self._vaults: dict[str, PIIVault] = {}
        self._lock = threading.Lock()

    def register(self, vault: PIIVault) -> None:
        with self._lock:
            self._vaults[vault.vault_id] = vault

    def get(self, vault_id: str) -> PIIVault | None:
        with self._lock:
            vault = self._vaults.get(vault_id)
            if vault and vault.is_expired:
                del self._vaults[vault_id]
                return None
            return vault

    def remove(self, vault_id: str) -> None:
        with self._lock:
            self._vaults.pop(vault_id, None)

    def cleanup_expired(self) -> None:
        with self._lock:
            expired = [vid for vid, v in self._vaults.items() if v.is_expired]
            for vid in expired:
                del self._vaults[vid]


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
    # Set inside the runner thread; used for real cancellation.
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)
    _state_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def set_state(self, new_state: SwarmState) -> None:
        with self._state_lock:
            self.state = new_state

    def get_state(self) -> SwarmState:
        with self._state_lock:
            return self.state


class RunGateway:
    """In-process adapter that drives the LangGraph engine.

    One instance is built at application startup (see ``composition``) and
    shared across all sessions. All public methods are thread-safe.

    Dynamic attributes wired by composition:
      ``intake_service``          — IntakeService
      ``make_graph_runner``       — factory → per-run async-iterable runner
      ``application_strategist``  — agent used for on-demand SOP drafts
      ``aclose_adapters``         — optional async cleanup for run-loop clients
    """

    intake_service: Any = None
    make_graph_runner: Any = None
    application_strategist: Any = None
    aclose_adapters: Any = None

    def __init__(
        self,
        graph_runner: Any | None = None,
        max_concurrent: int | None = None,
    ) -> None:
        cfg = get_settings()
        self._cfg = cfg
        self._max_concurrent = max_concurrent or getattr(cfg, "max_concurrent_runs", 3)
        self._graph_runner = graph_runner
        self._runs: dict[str, RunHandle] = {}
        self._lock = threading.Lock()
        self.vault_registry = VaultRegistry()

    # ── Public API ────────────────────────────────────────────────────────────

    def start_run(
        self,
        initial_state: SwarmState,
        *,
        is_replay: bool = False,
        graph_runner: Any | None = None,
        bus: EventBus | None = None,
    ) -> str:
        """Start a new run in a background thread. Returns run_id immediately."""
        self._cleanup_finished_runs()
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

        effective_bus.publish_sync(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="gateway",
            severity=EventSeverity.INFO,
            kind=EventKind.RUN_STARTED,
            payload={"message": "Run started", "is_replay": is_replay, "run_id": run_id},
        ))

        effective_runner = graph_runner if graph_runner is not None else self._graph_runner

        if effective_runner is not None:
            t = threading.Thread(
                target=self._run_in_thread,
                args=(handle, effective_runner, is_replay),
                name=f"sf-run-{run_id[:8]}",
                daemon=True,
            )
            handle._thread = t
            t.start()
            logger.info("RunGateway: started run %s in thread %s", run_id, t.name)
        else:
            logger.warning("RunGateway: no graph_runner configured — run %s is a stub", run_id)
            handle.finished = True

        return run_id

    def subscribe_events(self, run_id: str, cursor: int = 0) -> list[Event]:
        """Return all events after *cursor* for *run_id* (thread-safe, sync)."""
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

    def run_finished(self, run_id: str) -> bool:
        """True when the run's graph task has fully finished."""
        handle = self._runs.get(run_id)
        return bool(handle and handle.finished)

    def get_vault(self, run_id: str):
        """Return the PII vault for a run (display rehydration only)."""
        handle = self._runs.get(run_id)
        if handle is None:
            return None
        return self.vault_registry.get(handle.get_state().vault_id)

    def cancel(self, run_id: str) -> bool:
        """Cancel a running run: set the cancel event *and* kill the loop task."""
        handle = self._runs.get(run_id)
        if handle is None or handle.finished:
            return False

        handle.cancelled = True
        handle.cancel_event.set()

        # Real cancellation: interrupt the in-flight graph task on its loop.
        loop, task = handle._loop, handle._task
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # loop already closed

        handle.bus.publish_sync(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="gateway",
            severity=EventSeverity.WARNING,
            kind=EventKind.RUN_DEGRADED,
            payload={
                "message": "Run cancelled by user",
                "reason": "user_cancelled",
                "outcome": RunOutcome.FAILED_SAFE.value,
            },
        ))
        handle.finished = True
        logger.info("RunGateway: cancel requested for run %s", run_id)
        return True

    def request_sop(self, run_id: str, opportunity_id: str) -> bool:
        """Request an on-demand SOP draft for one opportunity.

        Spawns a dedicated thread (own loop + RunContext on the run's bus) that
        calls ``ApplicationStrategist.draft`` when the agent supports it.
        Returns False when the run or agent is unavailable.
        """
        handle = self._runs.get(run_id)
        strategist = self.application_strategist
        if handle is None or strategist is None or not hasattr(strategist, "draft"):
            logger.warning("request_sop: run %s or draft-capable strategist missing", run_id)
            return False

        state = handle.get_state()
        ctx = RunContext(
            run_id=run_id,
            bus=handle.bus,
            soft_deadline_s=self._cfg.run_soft_deadline_s,
            hard_deadline_s=self._cfg.run_hard_deadline_s,
            max_llm_calls=self._cfg.max_llm_calls_per_run,
            max_reasoning_calls=self._cfg.max_reasoning_calls_per_run,
            max_search_credits=self._cfg.max_search_credits_per_run,
            cancel_event=handle.cancel_event,
        )
        vault = self.vault_registry.get(state.vault_id)

        def _worker() -> None:
            token = set_context(ctx)
            try:
                try:
                    result = asyncio.run(strategist.draft(state, opportunity_id, vault=vault))
                except TypeError:
                    result = asyncio.run(strategist.draft(state, opportunity_id))
                pkg = result if isinstance(result, ApplicationPackage) else None
                if pkg is not None:
                    current = handle.get_state()
                    apps = dict(current.applications)
                    apps[opportunity_id] = pkg
                    handle.set_state(current.model_copy(update={"applications": apps}))
            except Exception:
                logger.exception("request_sop: draft failed for run %s", run_id)
            finally:
                reset_context(token)

        threading.Thread(target=_worker, name=f"sf-sop-{run_id[:8]}", daemon=True).start()
        return True

    def purge(self, run_id: str) -> bool:
        """Delete a run's state and its PII vault. Returns True when removed."""
        with self._lock:
            handle = self._runs.pop(run_id, None)
        if handle is None:
            return False
        self.vault_registry.remove(handle.get_state().vault_id)
        return True

    def list_runs(self) -> list[dict]:
        """Summary of all runs."""
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

    # ── SSE streaming ─────────────────────────────────────────────────────────

    async def subscribe_events_async(
        self, run_id: str, cursor: int = 0
    ) -> AsyncIterator[str]:
        """Async generator yielding SSE messages for a run.

        ``id:`` lines carry the event seq so clients can reconnect with
        ``?cursor=``. Keep-alive comments are sent every ~15 s of silence.
        All remaining events are drained after the run finishes before the
        ``complete`` message — the terminal event is never lost.
        """
        handle = self._runs.get(run_id)
        if not handle:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Run not found'})}\n\n"
            return

        while True:
            events = handle.bus.since(cursor)
            for event in events:
                cursor = event.seq
                yield f"id: {event.seq}\n{handle.bus.to_sse_message(event)}"

            if handle.finished or handle.bus.is_terminal():
                # Drain anything published between the last poll and finish.
                for event in handle.bus.since(cursor):
                    cursor = event.seq
                    yield f"id: {event.seq}\n{handle.bus.to_sse_message(event)}"
                break

            wait_start = time.monotonic()
            await handle.bus.wait_for_new_events(timeout=15.0)
            if not handle.bus.since(cursor) and time.monotonic() - wait_start >= 14.0:
                yield ": keep-alive\n\n"

        yield f"data: {json.dumps({'type': 'complete', 'run_id': run_id})}\n\n"

    # ── Runner thread ─────────────────────────────────────────────────────────

    def _run_in_thread(
        self,
        handle: RunHandle,
        graph_runner: Any,
        is_replay: bool,
    ) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        handle._loop = loop
        try:
            loop.run_until_complete(
                self._run_graph(handle, graph_runner=graph_runner, is_replay=is_replay)
            )
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            finally:
                loop.close()
                asyncio.set_event_loop(None)
                handle._loop = None
                handle._task = None

    async def _run_graph(
        self,
        handle: RunHandle,
        *,
        graph_runner: Any,
        is_replay: bool,
    ) -> None:
        """Drive the graph runner with hard-deadline enforcement and per-step snapshots."""
        run_id = handle.run_id
        deadlines = handle.get_state().run_meta.deadlines

        ctx = RunContext(
            run_id=run_id,
            bus=handle.bus,
            soft_deadline_s=deadlines.soft_s or self._cfg.run_soft_deadline_s,
            hard_deadline_s=deadlines.hard_s or self._cfg.run_hard_deadline_s,
            max_llm_calls=self._cfg.max_llm_calls_per_run,
            max_reasoning_calls=self._cfg.max_reasoning_calls_per_run,
            max_search_credits=self._cfg.max_search_credits_per_run,
            cancel_event=handle.cancel_event,
        )
        token = set_context(ctx)
        try:
            await asyncio.wait_for(
                self._drive(handle, graph_runner, is_replay, ctx),
                timeout=max(10.0, ctx.hard_deadline_s),
            )
            logger.info("RunGateway: run %s finished driving the graph", run_id)
            if not handle.bus.is_terminal():
                state = handle.get_state()
                handle.bus.publish_sync(Event(
                    run_id=run_id,
                    seq=0,
                    timestamp=datetime.now(timezone.utc),
                    node="gateway",
                    severity=EventSeverity.INFO,
                    kind=EventKind.RUN_COMPLETED,
                    payload={
                        "message": "Run completed",
                        "outcome": state.run_meta.outcome.value,
                    },
                ))

        except asyncio.TimeoutError:
            logger.warning("RunGateway: run %s hit hard deadline", run_id)
            self._mark_outcome(handle, RunOutcome.DEGRADED)
            handle.bus.publish_sync(Event(
                run_id=run_id,
                seq=0,
                timestamp=datetime.now(timezone.utc),
                node="gateway",
                severity=EventSeverity.WARNING,
                kind=EventKind.RUN_DEGRADED,
                payload={
                    "message": "Run hit the hard deadline — partial results kept",
                    "reason": "hard_deadline_exceeded",
                    "outcome": RunOutcome.DEGRADED.value,
                },
            ))

        except asyncio.CancelledError:
            logger.info("RunGateway: run %s cancelled", run_id)

        except Exception as exc:
            logger.exception("RunGateway: run %s failed: %s", run_id, exc)
            self._mark_outcome(handle, RunOutcome.FAILED_SAFE)
            handle.bus.publish_sync(Event(
                run_id=run_id,
                seq=0,
                timestamp=datetime.now(timezone.utc),
                node="gateway",
                severity=EventSeverity.ERROR,
                kind=EventKind.RUN_DEGRADED,
                payload={
                    "message": f"Run failed: {exc}",
                    "reason": str(exc),
                    "outcome": RunOutcome.FAILED_SAFE.value,
                },
            ))

        finally:
            reset_context(token)
            # Close per-loop adapter clients bound to this loop.
            closer = self.aclose_adapters
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    logger.debug("adapter cleanup failed", exc_info=True)
            handle.finished = True

    async def _drive(
        self,
        handle: RunHandle,
        graph_runner: Any,
        is_replay: bool,
        ctx: RunContext,
    ) -> None:
        """Consume the runner, snapshotting state on every streamed step.

        The runner may be an async generator of states (``graph.astream``),
        an awaitable resolving to a final state, or a sync function.
        """
        produced = graph_runner(handle.get_state(), handle.bus, is_replay=is_replay)

        if inspect.isasyncgen(produced) or hasattr(produced, "__aiter__"):
            async for values in produced:
                if handle.cancel_event.is_set():
                    raise asyncio.CancelledError()
                handle.set_state(_coerce_state(values))
            return

        if inspect.isawaitable(produced):
            result = await produced
            if result is not None:
                handle.set_state(_coerce_state(result))
            return

        if result_or_none := produced:  # plain sync return
            handle.set_state(_coerce_state(result_or_none))

    @staticmethod
    def _mark_outcome(handle: RunHandle, outcome: RunOutcome) -> None:
        state = handle.get_state()
        if state.run_meta.outcome == RunOutcome.IN_PROGRESS:
            meta = state.run_meta.model_copy(update={"outcome": outcome})
            handle.set_state(state.model_copy(update={"run_meta": meta}))

    def _cleanup_finished_runs(self) -> None:
        """Drop finished runs (and their vaults) past the vault TTL."""
        ttl = getattr(self._cfg, "pii_vault_ttl_seconds", 1800)
        now = datetime.now(timezone.utc)
        with self._lock:
            stale = [
                (rid, h.get_state().vault_id)
                for rid, h in self._runs.items()
                if h.finished
                and (now - h.started_at).total_seconds() > ttl
            ]
            for rid, _ in stale:
                del self._runs[rid]
        for _, vault_id in stale:
            if vault_id:
                self.vault_registry.remove(vault_id)
        self.vault_registry.cleanup_expired()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _coerce_state(values: Any) -> SwarmState:
    if isinstance(values, SwarmState):
        return values
    if isinstance(values, dict):
        return SwarmState.model_validate(values)
    raise TypeError(f"graph produced unsupported state type: {type(values)}")


def make_initial_state(run_id: str | None = None) -> SwarmState:
    """Create a blank SwarmState for a new run with defaults."""
    cfg = get_settings()
    rid = run_id or str(uuid.uuid4())
    return SwarmState(
        run_meta=RunMeta(
            run_id=rid,
            phase=RunPhase.INTAKE,
            deadlines=RunDeadlines(
                soft_s=cfg.run_soft_deadline_s,
                hard_s=cfg.run_hard_deadline_s,
            ),
        )
    )
