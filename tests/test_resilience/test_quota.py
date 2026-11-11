"""Tests for TokenBucket and ModelLimiter (per-model RPM / TPM / RPD)."""
from __future__ import annotations

import math
import time

from scholarship_finder.resilience.quota import ModelLimiter, TokenBucket


class TestTokenBucket:
    def test_starts_full(self):
        assert TokenBucket(rpm=60).tokens == 60.0

    def test_acquire_returns_zero_when_available(self):
        assert TokenBucket(rpm=60).acquire() == 0.0

    def test_acquire_returns_wait_when_empty(self):
        b = TokenBucket(rpm=1)
        b.acquire()
        assert b.acquire() > 0.0

    def test_refills_over_time(self):
        b = TokenBucket(rpm=60)
        while b.acquire() == 0.0:
            pass
        before = b.tokens
        time.sleep(0.12)
        b.wait_time()
        assert b.tokens > before

    def test_large_amount_wait(self):
        b = TokenBucket(rpm=8000)
        assert b.acquire(6000) == 0.0
        assert b.wait_time(6000) > 0.0

    def test_set_available_clamps(self):
        b = TokenBucket(rpm=8000)
        b.set_available(100)
        assert b.tokens <= 100.01


class TestModelLimiter:
    def test_tpm_wait_after_reserve(self):
        lim = ModelLimiter()
        lim.configure("m", rpm=30, tpm=8000, rpd=1000)
        assert lim.wait_time("m", 5000) == 0.0
        lim.reserve("m", 5000)
        assert lim.wait_time("m", 5000) > 0.0

    def test_rpd_exhaustion_is_infinite(self):
        lim = ModelLimiter()
        lim.configure("m", rpm=30, tpm=8000, rpd=2)
        lim.reserve("m", 10)
        lim.reserve("m", 10)
        assert math.isinf(lim.wait_time("m", 10))

    def test_observe_remaining_reduces_capacity(self):
        lim = ModelLimiter()
        lim.configure("m", rpm=30, tpm=8000, rpd=1000)
        lim.observe_remaining("m", 50)
        assert lim.wait_time("m", 4000) > 0.0

    def test_penalize_forces_wait(self):
        lim = ModelLimiter()
        lim.configure("m", rpm=30, tpm=8000, rpd=1000)
        lim.penalize("m", 5)
        assert lim.wait_time("m", 100) > 0.0

    def test_unknown_model_gets_defaults(self):
        assert ModelLimiter().wait_time("unknown", 100) == 0.0
