"""Per-run execution context, carried in a ContextVar.

Set by the RunGateway inside each run's background thread. Agents and the LLM
router use it to emit human-readable events, read the remaining time budget and
debit per-run budgets — without threading these through every call signature.
Everything degrades gracefully when no context is set (unit tests).
"""
from __future__ import annotations

import logging
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from scholarship_finder.domain.models import (
    Event,
    EventKind,
    EventSeverity,
    UsageRecord,
)

logger = logging.getLogger(__name__)


@dataclass
class RunContext:
    run_id: str
    bus: Any = None                       # EventBus (duck-typed: publish_sync)
    soft_deadline_s: float = 240.0
    hard_deadline_s: float = 360.0
    reserve_s: float = 90.0
    max_llm_calls: int = 60
    max_reasoning_calls: int = 6
    max_search_credits: int = 30
    started: float = field(default_factory=time.monotonic)
    usage: list[UsageRecord] = field(default_factory=list)
    llm_calls: int = 0
    reasoning_calls: int = 0
    search_credits: int = 0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── Time ────────────────────────────────────────────────────────────────
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def time_left(self) -> float:
        return max(0.0, self.hard_deadline_s - self.elapsed())

    def soft_time_left(self) -> float:
        return max(0.0, self.soft_deadline_s - self.elapsed())

    def soft_deadline_passed(self) -> bool:
        return self.elapsed() >= self.soft_deadline_s

    def discovery_time_left(self) -> float:
        """Budget for search/fetch/extraction — ranking/plan/SOP time reserved."""
        return max(0.0, self.hard_deadline_s - self.reserve_s - self.elapsed())

    def llm_usage_summary(self) -> str:
        """Per-model usage line: 'model: calls/tokens_in/tokens_out/tok-s'."""
        agg: dict[str, list[float]] = {}
        for u in self.usage:
            a = agg.setdefault(u.model, [0, 0, 0, 0, 0])
            a[0] += 1
            a[1] += u.input_tokens
            a[2] += u.output_tokens
            a[3] += 1 if u.outcome != "success" else 0
            a[4] += u.latency_ms
        parts = []
        for m, a in sorted(agg.items(), key=lambda kv: -kv[1][0]):
            toks = a[2] or 0
            tps = toks / max(a[4] / 1000.0, 0.01)
            parts.append(f"{m} {int(a[0])}c/{int(a[1])}in/{int(a[2])}out"
                         f"/{int(a[3])}err/{tps:.0f}tok-s")
        return " · ".join(parts) if parts else "no LLM calls"

    # ── Budgets ─────────────────────────────────────────────────────────────
    def try_debit_llm(self, reasoning: bool) -> tuple[bool, bool]:
        """Debit one LLM call. Returns (allowed, reasoning_allowed)."""
        with self._lock:
            if self.llm_calls >= self.max_llm_calls:
                return False, False
            reasoning_ok = reasoning and self.reasoning_calls < self.max_reasoning_calls
            self.llm_calls += 1
            if reasoning_ok:
                self.reasoning_calls += 1
            return True, reasoning_ok

    def try_debit_search(self, credits: int) -> bool:
        with self._lock:
            if self.search_credits + credits > self.max_search_credits:
                return False
            self.search_credits += credits
            return True

    def record_usage(self, record: UsageRecord) -> None:
        with self._lock:
            self.usage.append(record)

    def counters(self) -> dict[str, int]:
        return {
            "llm_calls": self.llm_calls,
            "reasoning_calls": self.reasoning_calls,
            "search_credits": self.search_credits,
        }


_current: ContextVar[RunContext | None] = ContextVar("scholarlift_run_context", default=None)


def set_context(ctx: RunContext | None):
    return _current.set(ctx)


def reset_context(token) -> None:
    _current.reset(token)


def current() -> RunContext | None:
    return _current.get()


def emit(
    node: str,
    message: str,
    kind: EventKind = EventKind.PROGRESS,
    severity: EventSeverity = EventSeverity.INFO,
    **payload: Any,
) -> None:
    """Publish a human-readable event for the current run (no-op without a context)."""
    ctx = _current.get()
    log = logger.warning if severity != EventSeverity.INFO else logger.info
    log("[%s] %s", node, message)
    if ctx is None or ctx.bus is None:
        return
    try:
        ctx.bus.publish_sync(Event(
            run_id=ctx.run_id,
            node=node,
            kind=kind,
            severity=severity,
            payload={"message": message, **payload},
        ))
    except Exception:  # pragma: no cover - telemetry must never break a run
        logger.debug("emit failed", exc_info=True)


def time_left(default: float = 999.0) -> float:
    ctx = _current.get()
    return ctx.time_left() if ctx else default


def soft_deadline_passed() -> bool:
    ctx = _current.get()
    return ctx.soft_deadline_passed() if ctx else False


def is_cancelled() -> bool:
    ctx = _current.get()
    return bool(ctx and ctx.cancel_event.is_set())
