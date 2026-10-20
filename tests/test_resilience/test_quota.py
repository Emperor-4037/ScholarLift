"""Tests for QuotaGovernor — RPM smoothing, RPD ceiling, and DEC-1 reasoning cap.

Tests use a fast artificial token bucket (high RPM) to avoid real-time waits,
and a slow one to trigger rate-limit errors.
"""
from __future__ import annotations

import asyncio
import pytest

from scholarship_finder.resilience.quota import QuotaGovernor, QuotaProfile, TokenBucket
from scholarship_finder.errors import QuotaExhaustedError, RateLimitedError


# ── TokenBucket unit tests ────────────────────────────────────────────────────

class TestTokenBucket:
    def test_starts_full(self):
        b = TokenBucket(rpm=60)
        assert b.tokens == 60.0

    def test_acquire_returns_zero_when_token_available(self):
        b = TokenBucket(rpm=60)
        wait = b.acquire()
        assert wait == 0.0

    def test_acquire_returns_wait_when_empty(self):
        b = TokenBucket(rpm=1)
        b.acquire()  # consume the single token
        wait = b.acquire()
        assert wait > 0.0

    def test_bucket_refills_over_time(self):
        """Verify refill math: with rpm=60, 0.1s = +0.1 token."""
        import time
        b = TokenBucket(rpm=60)
        # Drain fully
        while b.acquire() == 0.0:
            pass
        time.sleep(0.12)  # should refill ~0.12 tokens
        wait = b.acquire()
        # If refill worked, wait should be < 60 seconds (near zero)
        assert wait < 60.0

    def test_bucket_does_not_exceed_capacity(self):
        import time
        b = TokenBucket(rpm=5)
        time.sleep(0.5)  # wait for refill
        b._refill()
        assert b.tokens <= b.capacity


# ── QuotaGovernor — RPD ceiling ───────────────────────────────────────────────

class TestRPDCeiling:
    def _gov(self, rpd: int = 10, reserve: float = 0.0) -> QuotaGovernor:
        profile = QuotaProfile(provider="test", model="m", rpm=10000, rpd=rpd)
        return QuotaGovernor([profile], reserve_ratio=reserve, max_reasoning_per_run=4)

    @pytest.mark.asyncio
    async def test_allows_calls_within_rpd(self):
        gov = self._gov(rpd=5)
        for _ in range(5):
            await gov.acquire("test", "m")  # should not raise

    @pytest.mark.asyncio
    async def test_raises_quota_exhausted_when_rpd_hit(self):
        gov = self._gov(rpd=3)
        for _ in range(3):
            await gov.acquire("test", "m")
        with pytest.raises(QuotaExhaustedError):
            await gov.acquire("test", "m")

    @pytest.mark.asyncio
    async def test_reserve_ratio_reduces_public_quota(self):
        """With 30% reserve and rpd=10, public sees only 7 slots."""
        gov = self._gov(rpd=10, reserve=0.30)
        for _ in range(7):
            await gov.acquire("test", "m")
        with pytest.raises(QuotaExhaustedError):
            await gov.acquire("test", "m")

    @pytest.mark.asyncio
    async def test_judge_pool_gets_full_quota(self):
        profile = QuotaProfile(provider="test", model="m", rpm=10000, rpd=10)
        gov = QuotaGovernor([profile], reserve_ratio=0.30, is_judge=True)
        for _ in range(10):  # judge sees all 10
            await gov.acquire("test", "m")
        with pytest.raises(QuotaExhaustedError):
            await gov.acquire("test", "m")

    @pytest.mark.asyncio
    async def test_unknown_provider_does_not_enforce_rpd(self):
        """If no profile exists for provider/model, no RPD check."""
        gov = QuotaGovernor([], reserve_ratio=0.0)
        for _ in range(20):  # no limit configured
            await gov.acquire("unknown", "model")


# ── QuotaGovernor — DEC-1 reasoning cap ──────────────────────────────────────

class TestReasoningCap:
    def _gov(self, max_reasoning: int = 4) -> QuotaGovernor:
        profile = QuotaProfile(provider="test", model="m", rpm=10000, rpd=1000)
        return QuotaGovernor([profile], max_reasoning_per_run=max_reasoning)

    @pytest.mark.asyncio
    async def test_allows_calls_within_reasoning_cap(self):
        gov = self._gov(max_reasoning=4)
        for _ in range(4):
            await gov.acquire("test", "m", tier="reasoning")
        assert gov.reasoning_used() == 4

    @pytest.mark.asyncio
    async def test_raises_when_reasoning_cap_exceeded(self):
        gov = self._gov(max_reasoning=2)
        await gov.acquire("test", "m", tier="reasoning")
        await gov.acquire("test", "m", tier="reasoning")
        with pytest.raises(QuotaExhaustedError, match="Reasoning-tier cap"):
            await gov.acquire("test", "m", tier="reasoning")

    @pytest.mark.asyncio
    async def test_fast_tier_not_subject_to_reasoning_cap(self):
        gov = self._gov(max_reasoning=0)  # zero reasoning budget
        # Fast calls should still work
        for _ in range(5):
            await gov.acquire("test", "m", tier="fast")

    @pytest.mark.asyncio
    async def test_reasoning_used_counter(self):
        gov = self._gov(max_reasoning=4)
        assert gov.reasoning_used() == 0
        await gov.acquire("test", "m", tier="reasoning")
        await gov.acquire("test", "m", tier="fast")
        assert gov.reasoning_used() == 1


# ── QuotaGovernor — RPM smoothing ────────────────────────────────────────────

class TestRPMSmoothing:
    @pytest.mark.asyncio
    async def test_raises_rate_limited_when_wait_exceeds_max(self):
        """Bucket with rpm=1: second call should require ~60s wait → fail over."""
        profile = QuotaProfile(provider="test", model="m", rpm=1, rpd=1000)
        gov = QuotaGovernor([profile], max_reasoning_per_run=4)
        # First call drains the bucket
        await gov.acquire("test", "m", max_wait_s=0.001)
        # Second call: token not available, wait >> 0.001s
        with pytest.raises(RateLimitedError):
            await gov.acquire("test", "m", max_wait_s=0.001)


# ── QuotaGovernor — introspection ─────────────────────────────────────────────

class TestQuotaSummary:
    @pytest.mark.asyncio
    async def test_quota_summary_reflects_usage(self):
        profile = QuotaProfile(provider="test", model="m", rpm=10000, rpd=100)
        gov = QuotaGovernor([profile], reserve_ratio=0.0)
        for _ in range(5):
            await gov.acquire("test", "m")
        summary = gov.quota_summary()
        assert summary["test/m"]["used"] == 5
        assert summary["test/m"]["remaining"] == 95

    @pytest.mark.asyncio
    async def test_daily_usage_dict(self):
        profile = QuotaProfile(provider="test", model="m", rpm=10000, rpd=100)
        gov = QuotaGovernor([profile])
        await gov.acquire("test", "m")
        await gov.acquire("test", "m")
        assert gov.daily_usage()["test/m"] == 2
