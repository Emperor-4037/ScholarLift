"""Tests for LLMRouter — fake providers only, no network.

Covers: failover on rate-limit, TPM-wait model selection, reasoning-budget
downshift, and LLMUnavailableError with an attempts list.
"""
from __future__ import annotations

import pytest

from scholarship_finder.adapters.llm_router import LLMRouter
from scholarship_finder.domain.models import LLMTier
from scholarship_finder.engine.context import RunContext, set_context, reset_context
from scholarship_finder.errors import (
    LLMUnavailableError,
    RateLimitedError,
    RateLimitScope,
    TransientError,
)
from scholarship_finder.ports.llm import LLMResult, TaskSpec
from scholarship_finder.resilience.breaker import BreakerRegistry
from scholarship_finder.resilience.quota import ModelLimiter


class FakeSettings:
    """Minimal stand-in for Settings (tier chains + model limits)."""

    def __init__(self, fast=None, reasoning=None, vision=None,
                 limits=None):
        self._fast = fast or []
        self._reasoning = reasoning or []
        self._vision = vision or []
        self._limits = limits or {}

    def fast_models(self):
        return self._fast

    def reasoning_models(self):
        return self._reasoning

    def vision_models(self):
        return self._vision

    def model_limits(self, provider, model):
        return self._limits.get(model, {"rpm": 10_000, "tpm": 1_000_000, "rpd": 100_000})


class FakeProvider:
    """generate() pops the next scripted outcome for the model.

    Outcomes: LLMResult | Exception instance | callable(spec)->result.
    """

    def __init__(self, name, script):
        self.name = name
        self._script = dict(script)
        self.calls: list[tuple[str, TaskSpec]] = []

    def set_rate_observer(self, observer):
        self._observer = observer

    async def generate(self, model: str, spec: TaskSpec) -> LLMResult:
        self.calls.append((model, spec))
        queue = self._script.get(model, [])
        if not queue:
            return LLMResult(content="ok", provider=self.name, model=model, tier=spec.tier)
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(spec)
        return outcome


def _spec(tier=LLMTier.FAST) -> TaskSpec:
    return TaskSpec(tier=tier, system="sys", prompt="p", max_output_tokens=16)


def _router(settings, providers):
    return LLMRouter(
        settings=settings,
        providers=providers,
        limiter=ModelLimiter(),
        breakers=BreakerRegistry(),
    )


# ── Failover ──────────────────────────────────────────────────────────────────

async def test_failover_on_rate_limited():
    p = FakeProvider("groq", {
        "m1": [RateLimitedError("groq", "429", RateLimitScope.MINUTE, retry_after=30.0)],
        "m2": [LLMResult(content="hello", provider="groq", model="m2")],
    })
    router = _router(FakeSettings(fast=["m1", "m2"]), {"groq": p})

    result = await router.complete(_spec())
    assert result.content == "hello"
    assert result.model == "m2"
    assert result.fallback_used is True
    assert any("rate-limited" in a for a in result.attempts)


async def test_rate_limit_short_hint_same_model_retry():
    p = FakeProvider("groq", {
        "m1": [
            RateLimitedError("groq", "429", RateLimitScope.MINUTE, retry_after=0.05),
            LLMResult(content="recovered", provider="groq", model="m1"),
        ],
    })
    router = _router(FakeSettings(fast=["m1"]), {"groq": p})

    result = await router.complete(_spec())
    assert result.content == "recovered"
    assert [c[0] for c in p.calls].count("m1") == 2


async def test_transient_retried_once_then_fails_over():
    p = FakeProvider("groq", {
        "m1": [TransientError("groq", "boom"), TransientError("groq", "boom2")],
        "m2": [LLMResult(content="next", provider="groq", model="m2")],
    })
    router = _router(FakeSettings(fast=["m1", "m2"]), {"groq": p})

    result = await router.complete(_spec())
    assert result.model == "m2"
    assert len([c for c in p.calls if c[0] == "m1"]) == 2   # one retry


async def test_hung_model_times_out_once_then_is_deprioritised(monkeypatch):
    import asyncio

    from scholarship_finder.adapters import llm_router as lr
    monkeypatch.setattr(lr, "_FAST_TIMEOUT_MIN_S", 0.15)
    monkeypatch.setattr(lr, "_FAST_TIMEOUT_MAX_S", 0.15)
    hang = lambda spec: asyncio.sleep(30)  # noqa: E731

    class Hanging(FakeProvider):
        async def generate(self, model, spec):
            self.calls.append((model, spec))
            if model == "m1":
                await hang(spec)
            return LLMResult(content="ok", provider=self.name, model=model, tier=spec.tier)

    p = Hanging("groq", {})
    router = _router(FakeSettings(fast=["m1", "m2"]), {"groq": p})
    first = await router.complete(_spec())
    assert first.model == "m2"
    assert [c[0] for c in p.calls].count("m1") == 1, "a timeout must not trigger a same-model retry"
    second = await router.complete(_spec())
    assert second.model == "m2"
    assert [c[0] for c in p.calls].count("m1") == 1, "the hung model is tried last for a while"


# ── TPM wait choice ───────────────────────────────────────────────────────────

async def test_tpm_wait_skips_saturated_model():
    p = FakeProvider("groq", {
        "m2": [LLMResult(content="fast one", provider="groq", model="m2")],
    })
    settings = FakeSettings(fast=["m1", "m2"], limits={"m1": {"rpm": 10_000, "tpm": 100, "rpd": 100_000}})
    limiter = ModelLimiter()
    router = LLMRouter(settings=settings, providers={"groq": p},
                       limiter=limiter, breakers=BreakerRegistry())

    # Drain m1's tiny TPM bucket so its wait is minutes, not milliseconds.
    limiter.reserve("m1", 500)

    result = await router.complete(_spec())
    assert result.model == "m2"
    assert not [c for c in p.calls if c[0] == "m1"]
    # With ETA scheduling the saturated model is demoted, not skipped-with-wait:
    # the healthy model is tried first and wins before it is reached.
    assert result.model == "m2"


# ── Reasoning downshift ───────────────────────────────────────────────────────

async def test_reasoning_budget_exhausted_downshifts_to_fast():
    p = FakeProvider("groq", {
        "fast-m": [LLMResult(content="fast", provider="groq", model="fast-m")],
        "reason-m": [LLMResult(content="reason", provider="groq", model="reason-m")],
    })
    settings = FakeSettings(fast=["fast-m"], reasoning=["reason-m"])
    router = _router(settings, {"groq": p})

    ctx = RunContext(run_id="t", max_reasoning_calls=0, max_llm_calls=10)
    token = set_context(ctx)
    try:
        result = await router.complete(_spec(tier=LLMTier.REASONING))
    finally:
        reset_context(token)

    assert result.model == "fast-m"
    assert result.fallback_used is True
    assert not [c for c in p.calls if c[0] == "reason-m"]


# ── Total failure ─────────────────────────────────────────────────────────────

async def test_all_models_fail_raises_with_attempts():
    p = FakeProvider("groq", {
        "m1": [TransientError("groq", "b1"), TransientError("groq", "b2")],
        "m2": [TransientError("groq", "b3"), TransientError("groq", "b4")],
    })
    router = _router(FakeSettings(fast=["m1", "m2"]), {"groq": p})

    with pytest.raises(LLMUnavailableError) as exc_info:
        await router.complete(_spec())
    assert len(exc_info.value.attempts) >= 2
    assert any("m1" in a for a in exc_info.value.attempts)
    assert any("m2" in a for a in exc_info.value.attempts)


async def test_no_providers_raises_unavailable():
    router = _router(FakeSettings(fast=["m1"]), {})
    with pytest.raises(LLMUnavailableError):
        await router.complete(_spec())


async def test_available_flag():
    assert _router(FakeSettings(), {}).available() is False
    p = FakeProvider("groq", {})
    assert _router(FakeSettings(), {"groq": p}).available() is True


# ── complete_json ─────────────────────────────────────────────────────────────

async def test_complete_json_parses_and_retries_on_next_model():
    p = FakeProvider("groq", {
        "m1": [LLMResult(content="not json at all", provider="groq", model="m1")],
        "m2": [LLMResult(content='{"ok": true}', provider="groq", model="m2")],
    })
    router = _router(FakeSettings(fast=["m1", "m2"]), {"groq": p})

    result = await router.complete_json(_spec())
    assert result.model == "m2"
    assert result.parsed == {"ok": True}
