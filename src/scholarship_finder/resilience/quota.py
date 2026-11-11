"""Per-model rate limiting: requests/min, tokens/min and requests/day.

The LLM router asks the limiter how long it would have to wait before a call of
an estimated size fits a model's budget, and picks the model with the shortest
wait. Live rate-limit headers (Groq ``x-ratelimit-remaining-tokens``) correct the
local estimate so we never hammer a model that is already saturated.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


class TokenBucket:
    """Continuous-refill bucket: ``per_minute`` units per minute, burst = ``per_minute``."""

    def __init__(self, rpm: int) -> None:
        self.capacity = float(max(1, rpm))
        self.tokens: float = self.capacity
        self.refill_rate: float = self.capacity / 60.0     # units per second
        self._last_refill: float = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self._last_refill) * self.refill_rate)
        self._last_refill = now

    def wait_time(self, amount: float = 1.0) -> float:
        """Seconds until ``amount`` units are available (0 if available now)."""
        self._refill()
        amount = min(amount, self.capacity)
        if self.tokens >= amount:
            return 0.0
        return (amount - self.tokens) / self.refill_rate

    def acquire(self, amount: float = 1.0) -> float:
        """Consume ``amount`` if available and return 0.0; otherwise return the wait time."""
        wait = self.wait_time(amount)
        if wait == 0.0:
            self.tokens -= min(amount, self.capacity)
        return wait

    def force_consume(self, amount: float) -> None:
        """Consume even if it drives the bucket negative (used after an actual call)."""
        self._refill()
        self.tokens -= amount

    def set_available(self, amount: float) -> None:
        """Clamp the local estimate to a server-reported remaining value."""
        self._refill()
        self.tokens = min(self.tokens, float(amount))


@dataclass
class _ModelState:
    requests: TokenBucket
    tokens: TokenBucket
    rpd: int
    day_used: int = 0
    day_started: float = field(default_factory=time.monotonic)


class ModelLimiter:
    """Thread-safe per-model RPM / TPM / RPD bookkeeping."""

    def __init__(self) -> None:
        self._models: dict[str, _ModelState] = {}
        self._lock = threading.Lock()

    def configure(self, model: str, rpm: int, tpm: int, rpd: int) -> None:
        with self._lock:
            if model not in self._models:
                self._models[model] = _ModelState(TokenBucket(rpm), TokenBucket(tpm), rpd)

    def _state(self, model: str) -> _ModelState:
        if model not in self._models:
            self._models[model] = _ModelState(TokenBucket(60), TokenBucket(200_000), 10_000)
        st = self._models[model]
        if time.monotonic() - st.day_started > 86_400:
            st.day_used, st.day_started = 0, time.monotonic()
        return st

    def wait_time(self, model: str, est_tokens: int) -> float:
        """Seconds to wait before a call of ``est_tokens`` fits; ``inf`` if RPD exhausted."""
        with self._lock:
            st = self._state(model)
            if st.day_used >= st.rpd:
                return float("inf")
            return max(st.requests.wait_time(1), st.tokens.wait_time(est_tokens))

    def reserve(self, model: str, est_tokens: int) -> None:
        """Debit one request and the estimated tokens (call right before sending)."""
        with self._lock:
            st = self._state(model)
            st.requests.force_consume(1)
            st.tokens.force_consume(min(est_tokens, st.tokens.capacity))
            st.day_used += 1

    def observe_remaining(self, model: str, remaining_tokens: int | None) -> None:
        """Feed server-reported remaining tokens back into the local estimate."""
        if remaining_tokens is None:
            return
        with self._lock:
            self._state(model).tokens.set_available(remaining_tokens)

    def penalize(self, model: str, seconds: float) -> None:
        """After a 429, drain the token bucket so it refills in ``seconds``."""
        with self._lock:
            st = self._state(model)
            st.tokens.force_consume(st.tokens.tokens + st.tokens.refill_rate * max(seconds, 1.0))

    def mark_day_exhausted(self, model: str) -> None:
        with self._lock:
            st = self._state(model)
            st.day_used = st.rpd
