"""4-State quota-aware circuit breaker (architecture §3.4).

States: CLOSED → OPEN ↔ HALF_OPEN → KNOWN_EXHAUSTED
  CLOSED:          normal operation
  OPEN:            tripped; calls fail fast; resets after cooldown
  HALF_OPEN:       probe call allowed; success → CLOSED; failure → OPEN / KNOWN_EXHAUSTED
  KNOWN_EXHAUSTED: daily quota spent or model retired (404); never retried until reset clock

One breaker instance per (provider, model) key.
"""
from __future__ import annotations

import asyncio
import time
from enum import Enum

from scholarship_finder.errors import (
    AuthError,
    ModelUnavailableError,
    QuotaExhaustedError,
    RateLimitedError,
    RateLimitScope,
    TransientError,
    TimeoutError as SFTimeoutError,
    AdapterError,
)


class BreakerState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"
    KNOWN_EXHAUSTED = "KNOWN_EXHAUSTED"


class CircuitBreaker:
    """4-state circuit breaker keyed by (provider, model).

    Args:
        key:                 Identifier string e.g. "gemini/gemini-2.0-flash".
        failure_threshold:   Consecutive transient failures to trip CLOSED→OPEN.
        cooldown_s:          Seconds in OPEN before trying HALF_OPEN probe.
        quota_reset_hour:    UTC hour at which daily quota resets (default 0 = midnight).
    """

    def __init__(
        self,
        key: str,
        failure_threshold: int = 3,
        cooldown_s: float = 30.0,
        quota_reset_hour: int = 0,
    ) -> None:
        self.key = key
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self.quota_reset_hour = quota_reset_hour

        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._exhausted_permanent = False   # True for 404/retired models
        self._lock = asyncio.Lock()

    @property
    def state(self) -> BreakerState:
        # Promote OPEN → HALF_OPEN when cooldown elapsed
        if self._state == BreakerState.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self.cooldown_s:
                self._state = BreakerState.HALF_OPEN
        return self._state

    def is_open(self) -> bool:
        """True when calls should fail fast without attempting the adapter."""
        return self.state in (BreakerState.OPEN, BreakerState.KNOWN_EXHAUSTED)

    # ── State transitions ────────────────────────────────────────────────────

    def record_success(self) -> None:
        """Call this after a successful adapter response."""
        if self._state == BreakerState.HALF_OPEN:
            self._state = BreakerState.CLOSED
        self._consecutive_failures = 0

    def record_failure(self, error: AdapterError) -> None:
        """Update breaker state based on the error class."""
        if isinstance(error, (QuotaExhaustedError,)):
            self._state = BreakerState.KNOWN_EXHAUSTED
            self._exhausted_permanent = False  # resets at quota reset clock
            return

        if isinstance(error, ModelUnavailableError):
            self._state = BreakerState.KNOWN_EXHAUSTED
            self._exhausted_permanent = True   # 404 / retired — stays demoted
            return

        if isinstance(error, AuthError):
            # Auth failures open immediately without waiting for threshold
            self._state = BreakerState.OPEN
            self._opened_at = time.monotonic()
            return

        if isinstance(error, (TransientError, SFTimeoutError)):
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
            return

        if isinstance(error, RateLimitedError) and error.scope == RateLimitScope.MINUTE:
            if error.retry_after > 8.0:
                # Long hint — treat as transient trip
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.failure_threshold:
                    self._state = BreakerState.OPEN
                    self._opened_at = time.monotonic()
            # Short hint (≤8 s) handled by quota governor token bucket; don't trip

    def maybe_reset_quota(self) -> None:
        """Check if the daily quota reset clock has passed; restore CLOSED if so."""
        if self._state == BreakerState.KNOWN_EXHAUSTED and not self._exhausted_permanent:
            import datetime
            now = datetime.datetime.now(datetime.timezone.utc)
            current_hour = now.hour
            # Simple heuristic: if current UTC hour >= reset hour and we've been exhausted
            # for more than 1 hour, assume the quota has rolled over.
            if self._opened_at and (time.monotonic() - self._opened_at) > 3600:
                # Only reset if we've passed the reset hour since opening
                if current_hour >= self.quota_reset_hour:
                    self._state = BreakerState.CLOSED
                    self._consecutive_failures = 0
                    self._opened_at = None


class BreakerRegistry:
    """Process-wide registry of circuit breakers; one per (provider, model) key."""

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, provider: str, model: str) -> CircuitBreaker:
        key = f"{provider}/{model}"
        if key not in self._breakers:
            self._breakers[key] = CircuitBreaker(key)
        return self._breakers[key]

    def get_domain(self, domain: str) -> CircuitBreaker:
        """Per-domain breaker for Fetch tier anti-bot protection."""
        key = f"fetch/{domain}"
        if key not in self._breakers:
            self._breakers[key] = CircuitBreaker(key, failure_threshold=1, cooldown_s=60.0)
        return self._breakers[key]

    def all_states(self) -> dict[str, str]:
        return {k: b.state.value for k, b in self._breakers.items()}


# Process-level singleton
_registry = BreakerRegistry()


def get_breaker_registry() -> BreakerRegistry:
    return _registry
