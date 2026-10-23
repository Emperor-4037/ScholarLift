"""Middleware composition root — 6-layer decorator chain.

Architecture §3.4 stack (outermost → innermost):
    Telemetry → Cache → QuotaGovernor → CircuitBreaker → Retry → Timeout

``decorate()`` is the single entry point. It returns a wrapped async callable
that every port adapter should use instead of calling the provider SDK directly.

Usage
-----
    from scholarship_finder.resilience.middleware import decorate, MiddlewareConfig

    cfg = MiddlewareConfig(
        provider="gemini",
        model="gemini-2.0-flash-lite",
        tier="fast",
        timeout_s=30.0,
        cache=search_cache,          # TTLCache | None
        cache_key_fn=make_search_key,  # callable(*args, **kwargs) -> str | None
        governor=quota_governor,
        breaker=circuit_breaker,
        usage_records=run_usage_list,
    )
    wrapped = decorate(raw_adapter_fn, cfg)
    result = await wrapped(query)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

from scholarship_finder.domain.models import UsageRecord
from scholarship_finder.errors import (
    AdapterError,
    QuotaExhaustedError,
)
from scholarship_finder.resilience.breaker import CircuitBreaker
from scholarship_finder.resilience.cache import TTLCache, RequestCache
from scholarship_finder.resilience.quota import QuotaGovernor
from scholarship_finder.resilience.retry import with_retry
from scholarship_finder.resilience.timeout import with_timeout

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


@dataclass
class MiddlewareConfig:
    """Configuration for the 6-layer decorator chain.

    Parameters
    ----------
    provider:
        Adapter/vendor name, e.g. ``"gemini"``, ``"tavily"``, ``"jina"``.
    model:
        Specific model or endpoint ID — used as circuit-breaker key.
    tier:
        Capability tier: ``"fast"`` | ``"reasoning"`` | ``"vision"`` |
        ``"search"`` | ``"fetch"``. Controls quota enforcement.
    timeout_s:
        Hard per-call deadline in seconds.
    cache:
        Optional :class:`~scholarship_finder.resilience.cache.TTLCache` instance.
        Pass ``None`` to skip caching.
    cache_key_fn:
        Callable that receives ``(*args, **kwargs)`` and returns a string cache
        key, or ``None`` to skip caching for this specific call.
    request_cache:
        Optional :class:`~scholarship_finder.resilience.cache.RequestCache` instance.
        Per-run cache for duplicate lookups within single request.
    governor:
        Optional :class:`~scholarship_finder.resilience.quota.QuotaGovernor`.
    breaker:
        Optional :class:`~scholarship_finder.resilience.breaker.CircuitBreaker`.
    usage_records:
        Mutable list of :class:`~scholarship_finder.domain.models.UsageRecord`;
        the telemetry layer appends one entry per call.
    max_retries:
        Maximum retry attempts (passed to the retry layer).
    bypass_cache:
        If ``True``, skip read from cache (write-through still happens).
        Used by "Force Live Refresh" toggle.
    """

    provider: str
    model: str
    tier: str = "fast"
    timeout_s: float = 30.0
    cache: TTLCache | None = None
    cache_key_fn: Callable[..., str | None] | None = None
    request_cache: RequestCache | None = None
    governor: QuotaGovernor | None = None
    breaker: CircuitBreaker | None = None
    usage_records: list[UsageRecord] = field(default_factory=list)
    max_retries: int = 2
    bypass_cache: bool = False


def decorate(
    fn: Callable[..., Awaitable[_T]],
    cfg: MiddlewareConfig,
) -> Callable[..., Awaitable[_T]]:
    """Wrap *fn* with the full 6-layer resilience stack.

    The returned callable has the same signature as *fn*.
    All layers are composed in the correct order.
    """

    async def _wrapped(*args: Any, **kwargs: Any) -> _T:
        import time as _time
        start = _time.perf_counter()
        outcome = "success"
        cache_hit = False
        downshifted = kwargs.pop("_downshifted", False)

        # ── Layer 2: Cache (read) - check request cache first, then cross-run cache ─────────────────────────────────────────────
        cache_key: str | None = None
        if cfg.cache_key_fn is not None and not cfg.bypass_cache:
            try:
                cache_key = cfg.cache_key_fn(*args, **kwargs)
            except Exception:
                cache_key = None

            if cache_key:
                # Check request cache first (per-run, fastest)
                if cfg.request_cache is not None:
                    request_cached = cfg.request_cache.get(cache_key)
                    if request_cached is not None:
                        cache_hit = True
                        logger.debug("request cache hit | provider=%s key=%s", cfg.provider, cache_key[:80])
                        _record_usage(cfg, start, "success", cache_hit=True, downshifted=downshifted)
                        return request_cached  # type: ignore[return-value]

                # Check cross-run cache (slower but persists across runs)
                if cfg.cache is not None:
                    cached = cfg.cache.get(cache_key)
                    if cached is not None:
                        cache_hit = True
                        logger.debug("cache hit | provider=%s key=%s", cfg.provider, cache_key[:80])
                        _record_usage(cfg, start, "success", cache_hit=True, downshifted=downshifted)
                        return cached  # type: ignore[return-value]

        # ── Layer 3: QuotaGovernor ────────────────────────────────────────────
        if cfg.governor is not None:
            try:
                await cfg.governor.acquire(cfg.provider, cfg.model, tier=cfg.tier)
            except (QuotaExhaustedError, AdapterError) as exc:
                logger.warning("Quota denied | %s/%s: %s", cfg.provider, cfg.model, exc)
                outcome = type(exc).__name__
                _record_usage(cfg, start, outcome, downshifted=downshifted)
                raise

        # ── Layer 4: CircuitBreaker (fail-fast check) ─────────────────────────
        if cfg.breaker is not None and cfg.breaker.is_open():
            state = cfg.breaker.state
            msg = f"Circuit breaker {cfg.breaker.key} is {state.value} — fast-failing"
            logger.warning(msg)
            outcome = f"circuit_open:{state.value}"
            _record_usage(cfg, start, outcome, downshifted=downshifted)
            raise QuotaExhaustedError(provider=cfg.provider, message=msg)

        # ── Layers 5 + 6: Retry wrapping Timeout ─────────────────────────────
        async def _inner(*a: Any, **kw: Any) -> _T:
            return await with_timeout(
                fn(*a, **kw),
                seconds=cfg.timeout_s,
                label=f"{cfg.provider}/{cfg.model}",
            )

        try:
            result = await with_retry(
                _inner,
                *args,
                max_retries=cfg.max_retries,
                **kwargs,
            )
        except AdapterError as exc:
            outcome = type(exc).__name__
            # ── Layer 4b: CircuitBreaker (record failure) ─────────────────────
            if cfg.breaker is not None:
                cfg.breaker.record_failure(exc)
            _record_usage(cfg, start, outcome, downshifted=downshifted)
            raise
        except Exception as exc:
            outcome = type(exc).__name__
            _record_usage(cfg, start, outcome, downshifted=downshifted)
            raise

        # ── Layer 4c: CircuitBreaker (record success) ─────────────────────────
        if cfg.breaker is not None:
            cfg.breaker.record_success()

        # ── Layer 2: Cache (write) ────────────────────────────────────────────
        if cache_key and not cfg.bypass_cache:
            # Write to request cache (per-run)
            if cfg.request_cache is not None:
                cfg.request_cache.set(cache_key, result)
            # Write to cross-run cache
            if cfg.cache is not None:
                cfg.cache.set(cache_key, result)

        # ── Layer 1: Telemetry ────────────────────────────────────────────────
        _record_usage(cfg, start, outcome, cache_hit=cache_hit, downshifted=downshifted)
        return result

    _wrapped.__name__ = f"middleware({cfg.provider}/{cfg.model})"
    _wrapped.__qualname__ = _wrapped.__name__
    return _wrapped  # type: ignore[return-value]


def _record_usage(
    cfg: MiddlewareConfig,
    start: float,
    outcome: str,
    *,
    cache_hit: bool = False,
    downshifted: bool = False,
) -> None:
    """Append a UsageRecord to the shared list (Layer 1 — Telemetry)."""
    import time as _time
    from scholarship_finder.domain.models import LLMTier
    elapsed_ms = (_time.perf_counter() - start) * 1000.0
    tier_enum = LLMTier(cfg.tier) if isinstance(cfg.tier, str) else cfg.tier
    record = UsageRecord(
        provider=cfg.provider,
        model=cfg.model,
        tier=tier_enum,
        input_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        latency_ms=elapsed_ms,
        outcome="cache_hit" if cache_hit else outcome,
        downshifted=downshifted,
    )
    cfg.usage_records.append(record)
    logger.debug(
        "middleware | %s/%s tier=%s latency=%.1fms outcome=%s cache_hit=%s",
        cfg.provider,
        cfg.model,
        cfg.tier,
        elapsed_ms,
        record.outcome,
        cache_hit,
    )
