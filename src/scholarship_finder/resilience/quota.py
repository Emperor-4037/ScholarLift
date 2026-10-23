"""Token-bucket Quota Governor — rate-limit smoothing and budget enforcement.

Manages two dimensions per (provider, model):
  - RPM (requests per minute) — smoothed via a token bucket
  - RPD (requests per day)    — hard daily ceiling

Also tracks per-run reasoning-tier call budget (architecture invariant DEC-1: ≤4).

Architecture reference: §3.2, §3.7.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from scholarship_finder.errors import QuotaExhaustedError, RateLimitedError, RateLimitScope


@dataclass
class QuotaProfile:
    """Per-(provider, model) quota limits."""
    provider: str
    model: str
    rpm: int = 15          # requests per minute
    rpd: int = 500         # requests per day
    tpm: int = 1_000_000   # tokens per minute (informational; not enforced yet)
    # Pool: "public" consumes public quota; "judge" consumes from the reserve
    pool: str = "public"


class TokenBucket:
    """Leaky-bucket rate limiter for RPM smoothing.

    Fills at rate `rpm / 60` tokens per second up to capacity `rpm`.
    A request consumes one token; if empty, returns the wait time in seconds.
    """

    def __init__(self, rpm: int) -> None:
        self.capacity = max(1, rpm)
        self.tokens: float = float(self.capacity)
        self.refill_rate: float = rpm / 60.0     # tokens per second
        self._last_refill: float = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self._last_refill = now

    def acquire(self) -> float:
        """Attempt to consume one token.

        Returns:
            0.0 if a token was available (proceed immediately).
            >0.0 seconds to wait before the token will be available.
        """
        self._refill()
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0
        # Time until next token is available
        return (1.0 - self.tokens) / self.refill_rate


class QuotaGovernor:
    """Per-run and per-provider quota tracker.

    Responsibilities:
    - RPM smoothing via token buckets (wait ≤8 s; else signal fail-over)
    - RPD hard ceiling (raise QuotaExhaustedError when hit)
    - Per-run reasoning-tier call cap (DEC-1: ≤4)
    - Judge reserve: hold back `reserve_ratio` of RPD for judge pool
    """

    def __init__(
        self,
        profiles: list[QuotaProfile],
        reserve_ratio: float = 0.30,
        max_reasoning_per_run: int = 4,
        is_judge: bool = False,
    ) -> None:
        self._profiles: dict[str, QuotaProfile] = {
            f"{p.provider}/{p.model}": p for p in profiles
        }
        self._buckets: dict[str, TokenBucket] = {}
        self._daily_used: dict[str, int] = {}
        self._reserve_ratio = reserve_ratio
        self._max_reasoning = max_reasoning_per_run
        self._reasoning_used = 0
        self._is_judge = is_judge

    def _key(self, provider: str, model: str) -> str:
        return f"{provider}/{model}"

    def _bucket(self, key: str) -> TokenBucket:
        if key not in self._buckets:
            profile = self._profiles.get(key)
            rpm = profile.rpm if profile else 10
            self._buckets[key] = TokenBucket(rpm)
        return self._buckets[key]

    def _effective_rpd(self, profile: QuotaProfile) -> int:
        """RPD available to this session after reserving the judge pool."""
        if self._is_judge:
            return profile.rpd  # judge gets full quota
        return int(profile.rpd * (1.0 - self._reserve_ratio))

    async def acquire(
        self,
        provider: str,
        model: str,
        tier: str = "fast",
        max_wait_s: float = 8.0,
    ) -> None:
        """Block until a quota slot is available, or raise if limits are exceeded.

        Args:
            provider:   Adapter provider name e.g. "gemini".
            model:      Model ID.
            tier:       "reasoning" | "fast" | "vision".
            max_wait_s: If the token-bucket wait exceeds this, raise RateLimitedError
                        (caller will fail over to next chain entry).

        Raises:
            QuotaExhaustedError: Daily RPD reached.
            RateLimitedError:    RPM wait > max_wait_s.
        """
        key = self._key(provider, model)

        # ── DEC-1: reasoning cap ──────────────────────────────────────────
        if tier == "reasoning":
            if self._reasoning_used >= self._max_reasoning:
                raise QuotaExhaustedError(
                    provider,
                    f"Reasoning-tier cap reached: {self._reasoning_used}/{self._max_reasoning} "
                    "per-run calls used. Downshifting to fast tier.",
                )

        # ── RPD hard ceiling ──────────────────────────────────────────────
        profile = self._profiles.get(key)
        if profile:
            used = self._daily_used.get(key, 0)
            limit = self._effective_rpd(profile)
            if used >= limit:
                raise QuotaExhaustedError(
                    provider,
                    f"Daily quota exhausted for {model}: {used}/{limit} requests used.",
                )

        # ── RPM token bucket ──────────────────────────────────────────────
        wait = self._bucket(key).acquire()
        if wait > max_wait_s:
            raise RateLimitedError(
                provider,
                f"Rate limit wait {wait:.1f}s exceeds max_wait {max_wait_s}s for {model}",
                scope=RateLimitScope.MINUTE,
                retry_after=wait,
            )
        if wait > 0.0:
            await asyncio.sleep(wait)

        # ── Commit ────────────────────────────────────────────────────────
        self._daily_used[key] = self._daily_used.get(key, 0) + 1
        if tier == "reasoning":
            self._reasoning_used += 1

    def reasoning_used(self) -> int:
        return self._reasoning_used

    def daily_usage(self) -> dict[str, int]:
        return dict(self._daily_used)

    def quota_summary(self) -> dict[str, dict]:
        """For telemetry / pre-flight canary reporting."""
        summary: dict[str, dict] = {}
        for key, profile in self._profiles.items():
            used = self._daily_used.get(key, 0)
            limit = self._effective_rpd(profile)
            summary[key] = {
                "used": used,
                "limit": limit,
                "remaining": max(0, limit - used),
                "pct_used": round(used / limit * 100, 1) if limit else 0,
            }
        return summary
