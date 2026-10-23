"""Telemetry decorator — outermost layer of the 6-layer decorator chain.

Records per-call metrics (latency, tokens, cache_hit, outcome) into a list
of UsageRecord objects that callers can inspect or forward to the event bus.

Usage
-----
    from scholarship_finder.resilience.telemetry import telemetry_wrap

    wrapped = telemetry_wrap(
        fn=my_llm_call,
        provider="gemini",
        model="gemini-2.0-flash-lite",
        tier="fast",
        records=usage_records,  # list[UsageRecord] — mutated in place
    )
    result = await wrapped(task_spec)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, TypeVar

from scholarship_finder.domain.models import UsageRecord

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def telemetry_wrap(
    fn: Callable[..., Awaitable[_T]],
    *,
    provider: str,
    model: str,
    tier: str,
    records: list[UsageRecord],
    extra_tags: dict[str, str] | None = None,
) -> Callable[..., Awaitable[_T]]:
    """Wrap an async callable with latency / outcome telemetry.

    Parameters
    ----------
    fn:
        The async function to wrap.
    provider:
        Adapter name, e.g. ``"gemini"``, ``"tavily"``, ``"jina"``.
    model:
        Specific model or endpoint identifier used.
    tier:
        Capability tier: ``"fast"``, ``"reasoning"``, ``"vision"``, ``"search"``, ``"fetch"``.
    records:
        A **mutable** list that will have :class:`UsageRecord` instances appended
        after each call.
    extra_tags:
        Optional key/value metadata forwarded verbatim into the record.
    """
    tags = extra_tags or {}

    async def _wrapper(*args: Any, **kwargs: Any) -> _T:
        start = time.perf_counter()
        outcome = "success"
        downshifted = bool(kwargs.pop("_downshifted", False))

        try:
            result = await fn(*args, **kwargs)
            return result
        except Exception as exc:
            outcome = type(exc).__name__
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            # Pull token counts out of kwargs or result if available
            input_tokens: int = int(kwargs.get("_input_tokens", 0))
            output_tokens: int = int(kwargs.get("_output_tokens", 0))
            reasoning_tokens: int = int(kwargs.get("_reasoning_tokens", 0))

            # Convert tier string to LLMTier enum
            from scholarship_finder.domain.models import LLMTier
            tier_enum = LLMTier(tier) if isinstance(tier, str) else tier

            record = UsageRecord(
                provider=provider,
                model=model,
                tier=tier_enum,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                latency_ms=elapsed_ms,
                outcome=outcome,
                downshifted=downshifted,
            )
            records.append(record)
            logger.debug(
                "telemetry | provider=%s model=%s tier=%s latency=%.1fms outcome=%s tags=%s",
                provider,
                model,
                tier,
                elapsed_ms,
                outcome,
                tags,
            )

    return _wrapper  # type: ignore[return-value]


class TelemetryContext:
    """Thread/task-safe accumulator for a single run's usage records."""

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []
        self._lock = asyncio.Lock()

    async def append(self, record: UsageRecord) -> None:
        async with self._lock:
            self._records.append(record)

    @property
    def records(self) -> list[UsageRecord]:
        return list(self._records)

    def total_latency_ms(self) -> float:
        return sum(r.latency_ms for r in self._records)

    def total_tokens(self) -> dict[str, int]:
        return {
            "input": sum(r.input_tokens for r in self._records),
            "output": sum(r.output_tokens for r in self._records),
            "reasoning": sum(r.reasoning_tokens for r in self._records),
        }

    def success_rate(self) -> float:
        if not self._records:
            return 1.0
        return sum(1 for r in self._records if r.outcome == "success") / len(self._records)
