"""Tests for the 4-state circuit breaker (architecture §3.4).

State transitions tested:
  CLOSED → OPEN (on repeated TransientError)
  OPEN → HALF_OPEN (after cooldown)
  HALF_OPEN → CLOSED (on success)
  HALF_OPEN → OPEN (on failure)
  CLOSED → KNOWN_EXHAUSTED (on QuotaExhaustedError)
  CLOSED → KNOWN_EXHAUSTED (on ModelUnavailableError, permanent)
  CLOSED → OPEN (on AuthError, immediate open)
"""
from __future__ import annotations

import time
import pytest

from scholarship_finder.resilience.breaker import (
    BreakerState,
    CircuitBreaker,
    BreakerRegistry,
    get_breaker_registry,
)
from scholarship_finder.errors import (
    TransientError,
    QuotaExhaustedError,
    ModelUnavailableError,
    AuthError,
    RateLimitedError,
    RateLimitScope,
    TimeoutError as SFTimeoutError,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _breaker(threshold: int = 3, cooldown: float = 0.05) -> CircuitBreaker:
    return CircuitBreaker("test/model", failure_threshold=threshold, cooldown_s=cooldown)


def _transient() -> TransientError:
    return TransientError(provider="test", message="transient error")


def _timeout() -> SFTimeoutError:
    return SFTimeoutError(provider="test", message="timed out")


def _quota() -> QuotaExhaustedError:
    return QuotaExhaustedError(provider="test", message="quota done")


def _model_gone() -> ModelUnavailableError:
    return ModelUnavailableError(provider="test", message="model 404")


def _auth() -> AuthError:
    return AuthError(provider="test", message="bad key")


# ── Tests: initial state ──────────────────────────────────────────────────────

class TestInitialState:
    def test_starts_closed(self):
        b = _breaker()
        assert b.state == BreakerState.CLOSED

    def test_not_open_initially(self):
        b = _breaker()
        assert not b.is_open()


# ── Tests: CLOSED → OPEN ──────────────────────────────────────────────────────

class TestTripToOpen:
    def test_transient_trips_after_threshold(self):
        b = _breaker(threshold=3)
        for _ in range(2):
            b.record_failure(_transient())
        assert b.state == BreakerState.CLOSED  # not yet

        b.record_failure(_transient())
        assert b.state == BreakerState.OPEN
        assert b.is_open()

    def test_timeout_also_counts_as_transient(self):
        b = _breaker(threshold=2)
        b.record_failure(_timeout())
        assert b.state == BreakerState.CLOSED
        b.record_failure(_timeout())
        assert b.state == BreakerState.OPEN

    def test_auth_error_opens_immediately(self):
        b = _breaker(threshold=10)
        b.record_failure(_auth())
        assert b.state == BreakerState.OPEN

    def test_success_resets_consecutive_count(self):
        b = _breaker(threshold=3)
        b.record_failure(_transient())
        b.record_failure(_transient())
        b.record_success()
        # Counter should be reset; two more failures needed
        b.record_failure(_transient())
        b.record_failure(_transient())
        assert b.state == BreakerState.CLOSED  # still need one more


# ── Tests: OPEN → HALF_OPEN ───────────────────────────────────────────────────

class TestCooldown:
    def test_open_transitions_to_half_open_after_cooldown(self):
        b = _breaker(cooldown=0.05)
        b.record_failure(_auth())  # immediate open
        assert b.state == BreakerState.OPEN

        time.sleep(0.1)  # wait for cooldown
        assert b.state == BreakerState.HALF_OPEN
        assert not b.is_open()  # HALF_OPEN allows probe call

    def test_open_stays_open_before_cooldown(self):
        b = _breaker(cooldown=60.0)
        b.record_failure(_auth())
        # should still be OPEN immediately
        assert b.state == BreakerState.OPEN


# ── Tests: HALF_OPEN transitions ──────────────────────────────────────────────

class TestHalfOpen:
    def _get_half_open(self) -> CircuitBreaker:
        b = _breaker(threshold=1, cooldown=0.05)
        b.record_failure(_transient())
        assert b.state == BreakerState.OPEN
        time.sleep(0.1)
        assert b.state == BreakerState.HALF_OPEN
        return b

    def test_half_open_success_restores_closed(self):
        b = self._get_half_open()
        b.record_success()
        assert b.state == BreakerState.CLOSED

    def test_half_open_failure_reopens(self):
        b = self._get_half_open()
        b.record_failure(_transient())
        assert b.state == BreakerState.OPEN


# ── Tests: KNOWN_EXHAUSTED ────────────────────────────────────────────────────

class TestKnownExhausted:
    def test_quota_exhausted_goes_to_known_exhausted(self):
        b = _breaker()
        b.record_failure(_quota())
        assert b.state == BreakerState.KNOWN_EXHAUSTED
        assert b.is_open()
        assert not b._exhausted_permanent

    def test_model_unavailable_is_permanent(self):
        b = _breaker()
        b.record_failure(_model_gone())
        assert b.state == BreakerState.KNOWN_EXHAUSTED
        assert b._exhausted_permanent

    def test_known_exhausted_can_reset_if_not_permanent(self):
        b = _breaker()
        b.record_failure(_quota())
        # Simulate that quota has been exhausted for > 1 hour
        b._opened_at = time.monotonic() - 3601
        b.maybe_reset_quota()
        assert b.state == BreakerState.CLOSED

    def test_permanent_exhaustion_does_not_reset(self):
        b = _breaker()
        b.record_failure(_model_gone())
        b._opened_at = time.monotonic() - 3601
        b.maybe_reset_quota()
        assert b.state == BreakerState.KNOWN_EXHAUSTED  # still


# ── Tests: RateLimitedError ───────────────────────────────────────────────────

class TestRateLimitedBehavior:
    def test_long_rate_limit_hint_contributes_to_failure_count(self):
        b = _breaker(threshold=2)
        err = RateLimitedError("test", "rate limited", scope=RateLimitScope.MINUTE, retry_after=15.0)
        b.record_failure(err)
        assert b.state == BreakerState.CLOSED
        b.record_failure(err)
        assert b.state == BreakerState.OPEN

    def test_short_rate_limit_hint_does_not_trip_breaker(self):
        b = _breaker(threshold=2)
        err = RateLimitedError("test", "rate limited", scope=RateLimitScope.MINUTE, retry_after=3.0)
        for _ in range(5):
            b.record_failure(err)
        assert b.state == BreakerState.CLOSED  # short hint — don't trip


# ── Tests: BreakerRegistry ────────────────────────────────────────────────────

class TestBreakerRegistry:
    def test_get_returns_same_instance(self):
        reg = BreakerRegistry()
        b1 = reg.get("gemini", "flash")
        b2 = reg.get("gemini", "flash")
        assert b1 is b2

    def test_get_different_models_returns_different_breakers(self):
        reg = BreakerRegistry()
        b1 = reg.get("gemini", "flash")
        b2 = reg.get("gemini", "flash-lite")
        assert b1 is not b2

    def test_get_domain_returns_per_domain_breaker(self):
        reg = BreakerRegistry()
        b = reg.get_domain("example.com")
        assert "fetch/example.com" in reg.all_states()

    def test_all_states_reflects_state_changes(self):
        reg = BreakerRegistry()
        b = reg.get("openai", "gpt4")
        b.record_failure(_auth())
        states = reg.all_states()
        assert states["openai/gpt4"] == BreakerState.OPEN.value

    def test_process_singleton(self):
        r1 = get_breaker_registry()
        r2 = get_breaker_registry()
        assert r1 is r2
