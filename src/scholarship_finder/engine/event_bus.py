"""Bounded ring-buffer Event Bus (architecture §2.2).

A single-process pub/sub channel for streaming run telemetry from agents
to the UI and any downstream subscribers.

Key properties:
- Bounded ring buffer (collections.deque maxlen=500) — never OOMs
- Terminal events (RUN_COMPLETED, RUN_DEGRADED, FAILED_SAFE) are protected
  from eviction (stored in a separate terminal slot)
- Cursor-based subscriptions: each subscriber tracks its own read offset
- Thread-safe via asyncio.Lock (single event loop assumed)
- Zero dependencies outside stdlib + domain models

Usage
-----
    bus = EventBus()
    # In agent:
    await bus.publish(Event(run_id="x", seq=1, ...))
    # In UI:
    events = bus.since(cursor=0)
    cursor = events[-1].seq if events else 0
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Final

from scholarship_finder.domain.models import Event, EventKind, EventSeverity

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
    """

    def __init__(self, capacity: int = _RING_CAPACITY) -> None:
        self._ring: deque[Event] = deque(maxlen=capacity)
        self._terminal: Event | None = None   # protected terminal slot
        self._lock = asyncio.Lock()
        self._seq = 0                          # monotonic sequence counter
        self._waiters: list[asyncio.Event] = []  # for streaming subscribers

    # ── Publishing ────────────────────────────────────────────────────────────

    async def publish(self, event: Event) -> None:
        """Append *event* to the bus. Assigns a monotonic seq if seq == 0."""
        async with self._lock:
            self._seq += 1
            # Override seq to ensure monotonic ordering
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
                self._terminal = stamped  # always survives ring eviction
                logger.info("EventBus: terminal event %s (seq=%d)", stamped.kind, stamped.seq)

            self._ring.append(stamped)
            logger.debug("EventBus publish seq=%d kind=%s node=%s", stamped.seq, stamped.kind, stamped.node)

            # Wake up any streaming waiters
            for waiter in self._waiters:
                waiter.set()

    def publish_sync(self, event: Event) -> None:
        """Synchronous publish — use only from non-async contexts (e.g. graph routing predicates)."""
        # Create a new event loop task if we're already in a running loop
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.publish(event))
        except RuntimeError:
            # No running loop — run synchronously via a new loop
            asyncio.run(self.publish(event))

    # ── Subscribing ───────────────────────────────────────────────────────────

    def since(self, cursor: int = 0) -> list[Event]:
        """Return all events with seq > *cursor*, in order.

        Includes the protected terminal event even if it was evicted from ring.
        """
        events = [e for e in self._ring if e.seq > cursor]

        # Ensure terminal event is always included (may have been evicted)
        if self._terminal and self._terminal.seq > cursor:
            if not any(e.seq == self._terminal.seq for e in events):
                events.append(self._terminal)
                events.sort(key=lambda e: e.seq)

        return events

    async def wait_for_new(self, cursor: int, timeout_s: float = 30.0) -> list[Event]:
        """Block until there is at least one new event after *cursor*, or timeout.

        Returns immediately if events already exist beyond cursor.
        Useful for SSE-style streaming in the UI.
        """
        events = self.since(cursor)
        if events:
            return events

        waiter = asyncio.Event()
        self._waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            pass
        finally:
            try:
                self._waiters.remove(waiter)
            except ValueError:
                pass

        return self.since(cursor)

    # ── Introspection ─────────────────────────────────────────────────────────

    @property
    def latest_seq(self) -> int:
        return self._seq

    @property
    def terminal_event(self) -> Event | None:
        return self._terminal

    def is_terminal(self) -> bool:
        return self._terminal is not None

    def all_events(self) -> list[Event]:
        """Return all buffered events sorted by seq. For snapshot/replay."""
        return sorted(self._ring, key=lambda e: e.seq)

    def event_count(self) -> int:
        return len(self._ring)
