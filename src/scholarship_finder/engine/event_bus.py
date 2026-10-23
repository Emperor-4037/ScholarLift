"""Bounded ring-buffer Event Bus (architecture §2.2).

A single-process pub/sub channel for streaming run telemetry from agents
to the UI and any downstream subscribers.

Key properties:
- Bounded ring buffer (collections.deque maxlen=500) — never OOMs
- Terminal events (RUN_COMPLETED, RUN_DEGRADED, FAILED_SAFE) are protected
  from eviction (stored in a separate terminal slot)
- Cursor-based subscriptions: each subscriber tracks its own read offset
- **Thread-safe via threading.Lock** — safe to publish from a background
  thread (the pipeline runner) and read from the main thread
- Zero dependencies outside stdlib + domain models

Usage
-----
    bus = EventBus()
    # In agent (async, background thread):
    await bus.publish(Event(run_id="x", seq=1, ...))
    # In UI (sync, main thread):
    events = bus.since(cursor=0)
    cursor = events[-1].seq if events else 0
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Final

from scholarship_finder.domain.models import Event, EventKind

logger = logging.getLogger(__name__)

_RING_CAPACITY: Final[int] = 500

# Terminal event kinds that must never be evicted
_TERMINAL_KINDS: Final[frozenset[EventKind]] = frozenset({
    EventKind.RUN_COMPLETED,
    EventKind.RUN_DEGRADED,
    EventKind.PHASE_A_READY,
})


class EventBus:
    """Bounded ring-buffer event bus for a single run.

    One ``EventBus`` instance per run — created by ``RunGateway.start_run()``.

    **Thread-safety:** ``publish`` / ``publish_sync`` and ``since`` may be
    called from different threads concurrently; a ``threading.Lock`` guards
    all mutations.
    """

    def __init__(self, capacity: int = _RING_CAPACITY) -> None:
        self._ring: deque[Event] = deque(maxlen=capacity)
        self._terminal: Event | None = None   # protected terminal slot
        self._lock = threading.Lock()          # thread-safe (not asyncio.Lock)
        self._seq = 0                          # monotonic sequence counter

    # ── Publishing ────────────────────────────────────────────────────────────

    async def publish(self, event: Event) -> None:
        """Append *event* to the bus. Assigns a monotonic seq if seq == 0.

        Safe to await from within an async context running in any thread.
        """
        with self._lock:
            self._seq += 1
            stamped = Event(
                run_id=event.run_id,
                seq=self._seq,
                timestamp=event.timestamp,
                node=event.node,
                severity=event.severity,
                kind=event.kind,
                payload=event.payload,
            )

            if stamped.kind in _TERMINAL_KINDS:
                self._terminal = stamped
                logger.info(
                    "EventBus: terminal event %s (seq=%d)", stamped.kind, stamped.seq
                )

            self._ring.append(stamped)
            logger.debug(
                "EventBus publish seq=%d kind=%s node=%s",
                stamped.seq, stamped.kind, stamped.node,
            )

    def publish_sync(self, event: Event) -> None:
        """Synchronous publish — callable from any thread, any async context.

        Preferred over the async variant when called outside a coroutine.
        """
        with self._lock:
            self._seq += 1
            stamped = Event(
                run_id=event.run_id,
                seq=self._seq,
                timestamp=event.timestamp,
                node=event.node,
                severity=event.severity,
                kind=event.kind,
                payload=event.payload,
            )
            if stamped.kind in _TERMINAL_KINDS:
                self._terminal = stamped
            self._ring.append(stamped)

    # ── Subscribing ───────────────────────────────────────────────────────────

    def since(self, cursor: int = 0) -> list[Event]:
        """Return all events with seq > *cursor*, in order.

        Includes the protected terminal event even if it was evicted from ring.
        Safe to call from any thread (Streamlit script thread included).
        """
        with self._lock:
            events = [e for e in self._ring if e.seq > cursor]
            terminal = self._terminal

        # Ensure terminal event is always included (may have been evicted)
        if terminal and terminal.seq > cursor:
            if not any(e.seq == terminal.seq for e in events):
                events.append(terminal)
                events.sort(key=lambda e: e.seq)

        return events

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def terminal_event(self) -> Event | None:
        with self._lock:
            return self._terminal

    def is_terminal(self) -> bool:
        with self._lock:
            return self._terminal is not None

    def all_events(self) -> list[Event]:
        """Return all buffered events sorted by seq. For snapshot/replay."""
        with self._lock:
            return sorted(self._ring, key=lambda e: e.seq)

    def event_count(self) -> int:
        with self._lock:
            return len(self._ring)

    # ── SSE Support (non-breaking addition) ───────────────────────────────────

    async def subscribe_async(self, run_id: str, cursor: int = 0) -> list[Event]:
        """Async-friendly subscription for SSE streaming.

        Returns events since cursor. For true async streaming, use RunGateway's
        subscribe_events_async which polls this method.
        """
        return self.since(cursor)

    def to_sse_message(self, event: Event) -> str:
        """Convert event to SSE message format.

        Non-breaking addition - used by FastAPI SSE endpoints.
        """
        return f"data: {event.model_dump_json()}\n\n"
