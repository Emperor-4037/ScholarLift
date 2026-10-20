"""Integration tests for the 6-layer middleware composition stack.

All tests use mock adapter functions — zero live API calls.
"""
from __future__ import annotations

import asyncio
import pytest

from scholarship_finder.resilience.breaker import CircuitBreaker, BreakerState
from scholarship_finder.resilience.cache import TTLCache
from scholarship_finder.resilience.quota import QuotaGovernor, QuotaProfile
from scholarship_finder.resilience.middleware import MiddlewareConfig, decorate
from scholarship_finder.errors import (
    QuotaExhaustedError,
    TransientError,
    TimeoutError as SFTimeoutError,
)
from scholarship_finder.domain.models import UsageRecord


# ── Helpers ───────────────────────────────────────────────────────────────────

def _profile(rpm: int = 10000, rpd: int = 1000) -> QuotaProfile:
    return QuotaProfile(provider="test", model="m", rpm=rpm, rpd=rpd)


def _cfg(
    *,
    cache: TTLCache | None = None,
    cache_key_fn=None,
    governor: QuotaGovernor | None = None,
    breaker: CircuitBreaker | None = None,
    timeout_s: float = 5.0,
    max_retries: int = 0,
    bypass_cache: bool = False,
) -> tuple[MiddlewareConfig, list[UsageRecord]]:
    records: list[UsageRecord] = []
    cfg = MiddlewareConfig(
        provider="test",
        model="m",
        tier="fast",
        timeout_s=timeout_s,
        cache=cache,
        cache_key_fn=cache_key_fn,
        governor=governor,
        breaker=breaker,
        usage_records=records,
        max_retries=max_retries,
        bypass_cache=bypass_cache,
    )
    return cfg, records


# ── Tests: happy path ─────────────────────────────────────────────────────────

class TestHappyPath:
    @pytest.mark.asyncio
    async def test_passes_through_result(self):
        async def fn(x): return x * 2
        cfg, records = _cfg()
        wrapped = decorate(fn, cfg)
        assert await wrapped(21) == 42

    @pytest.mark.asyncio
    async def test_records_usage_on_success(self):
        async def fn(): return "ok"
        cfg, records = _cfg()
        wrapped = decorate(fn, cfg)
        await wrapped()
        assert len(records) == 1
        assert records[0].outcome == "success"

    @pytest.mark.asyncio
    async def test_records_provider_and_model(self):
        async def fn(): return True
        cfg, records = _cfg()
        wrapped = decorate(fn, cfg)
        await wrapped()
        assert records[0].provider == "test"
        assert records[0].model == "m"


# ── Tests: cache layer ────────────────────────────────────────────────────────

class TestCacheLayer:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_fn(self):
        call_count = 0

        async def fn(q):
            nonlocal call_count
            call_count += 1
            return f"result:{q}"

        cache = TTLCache(maxsize=10, ttl_s=60)
        cfg, records = _cfg(cache=cache, cache_key_fn=lambda q: f"key:{q}")
        wrapped = decorate(fn, cfg)

        r1 = await wrapped("hello")
        r2 = await wrapped("hello")  # should hit cache
        assert r1 == r2 == "result:hello"
        assert call_count == 1  # fn called only once

    @pytest.mark.asyncio
    async def test_cache_miss_calls_fn_and_stores(self):
        cache = TTLCache(maxsize=10, ttl_s=60)
        cfg, records = _cfg(cache=cache, cache_key_fn=lambda q: f"key:{q}")

        async def fn(q): return f"fresh:{q}"
        wrapped = decorate(fn, cfg)
        result = await wrapped("test")
        assert result == "fresh:test"
        assert cache.get("key:test") == "fresh:test"

    @pytest.mark.asyncio
    async def test_bypass_cache_skips_read(self):
        cache = TTLCache(maxsize=10, ttl_s=60)
        cache.set("key:x", "stale")
        call_count = 0

        async def fn(q):
            nonlocal call_count
            call_count += 1
            return "fresh"

        cfg, _ = _cfg(cache=cache, cache_key_fn=lambda q: f"key:{q}", bypass_cache=True)
        wrapped = decorate(fn, cfg)
        result = await wrapped("x")
        assert result == "fresh"
        assert call_count == 1


# ── Tests: quota layer ────────────────────────────────────────────────────────

class TestQuotaLayer:
    @pytest.mark.asyncio
    async def test_quota_exhausted_raises_and_records(self):
        gov = QuotaGovernor([_profile(rpd=0)], reserve_ratio=0.0)
        cfg, records = _cfg(governor=gov)

        async def fn(): return "ok"
        wrapped = decorate(fn, cfg)
        with pytest.raises(QuotaExhaustedError):
            await wrapped()
        assert len(records) == 1
        assert "QuotaExhaustedError" in records[0].outcome


# ── Tests: circuit breaker layer ─────────────────────────────────────────────

class TestCircuitBreakerLayer:
    @pytest.mark.asyncio
    async def test_open_breaker_fast_fails(self):
        breaker = CircuitBreaker("test/m", failure_threshold=1, cooldown_s=60)
        from scholarship_finder.errors import AuthError
        breaker.record_failure(AuthError(provider="test", message="bad"))
        assert breaker.is_open()

        cfg, records = _cfg(breaker=breaker)

        async def fn(): return "ok"
        wrapped = decorate(fn, cfg)
        with pytest.raises(QuotaExhaustedError):
            await wrapped()
        assert "circuit_open" in records[0].outcome

    @pytest.mark.asyncio
    async def test_success_records_success_to_breaker(self):
        breaker = CircuitBreaker("test/m")
        cfg, _ = _cfg(breaker=breaker)

        async def fn(): return "ok"
        wrapped = decorate(fn, cfg)
        await wrapped()
        assert breaker.state == BreakerState.CLOSED


# ── Tests: timeout layer ──────────────────────────────────────────────────────

class TestTimeoutLayer:
    @pytest.mark.asyncio
    async def test_slow_fn_raises_timeout(self):
        async def slow_fn():
            await asyncio.sleep(5.0)
            return "late"

        cfg, records = _cfg(timeout_s=0.05)
        wrapped = decorate(slow_fn, cfg)
        with pytest.raises(SFTimeoutError):
            await wrapped()

    @pytest.mark.asyncio
    async def test_fast_fn_completes_within_timeout(self):
        async def fast_fn(): return "quick"
        cfg, _ = _cfg(timeout_s=5.0)
        wrapped = decorate(fast_fn, cfg)
        assert await wrapped() == "quick"


# ── Tests: retry layer ────────────────────────────────────────────────────────

class TestRetryLayer:
    @pytest.mark.asyncio
    async def test_transient_error_retried(self):
        attempts = 0

        async def flaky():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise TransientError(provider="test", message="transient")
            return "ok"

        cfg, _ = _cfg(max_retries=3)
        wrapped = decorate(flaky, cfg)
        result = await wrapped()
        assert result == "ok"
        assert attempts == 3
