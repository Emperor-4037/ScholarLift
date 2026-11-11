"""LLM router — implements the LLMPort on top of per-vendor providers.

The router owns all resilience policy for LLM calls:

- ordered per-tier model chains (from ``Settings``),
- per-model :class:`CircuitBreaker` (shared ``BreakerRegistry``),
- per-model RPM/TPM token buckets + RPD counter (:class:`ModelLimiter`),
- per-run budgets from the ambient :class:`RunContext` (LLM calls,
  reasoning calls → downshift to the fast chain),
- failover events + ``UsageRecord`` bookkeeping.

Model IDs never leak into agents — they only see tiers.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
import time
from typing import Any

from scholarship_finder.adapters.llm_json import parse_json_lenient
from scholarship_finder.domain.models import EventKind, EventSeverity, LLMTier, UsageRecord
from scholarship_finder.engine import context as run_context
from scholarship_finder.errors import (
    AdapterError,
    AuthError,
    ContextTooLongError,
    LLMUnavailableError,
    ModelUnavailableError,
    QuotaExhaustedError,
    RateLimitedError,
    SchemaViolationError,
    TransientError,
)
from scholarship_finder.ports.llm import LLMResult, TaskSpec
from scholarship_finder.resilience.breaker import BreakerRegistry, get_breaker_registry
from scholarship_finder.resilience.quota import ModelLimiter

logger = logging.getLogger(__name__)

_FAST_TIMEOUT_MIN_S = 14.0
_FAST_TIMEOUT_MAX_S = 40.0

# A model whose limiter says "wait ≤ this" is used immediately.
_MAX_IMMEDIATE_WAIT_S = 4.0
# Cap on how long a single provider call may take before we treat it as transient.
_CALL_TIMEOUT_S = 90.0
# 429s with a hint ≤ this get one same-model retry.
_SAME_MODEL_RETRY_HINT_S = 5.0
# Hard bound on the "sleep for the shortest wait" fallback pass.
_MAX_DEFERRED_WAIT_S = 30.0


class LLMRouter:
    """Vendor-neutral LLM gateway implementing ``LLMPort``.

    Args:
        settings:  Settings instance (tier chains, per-model limits).
        providers: ``{provider_name: provider}`` — each provider has
                   ``async generate(model, spec) -> LLMResult``.
        limiter:   Shared ModelLimiter (RPM/TPM/RPD per model).
        breakers:  Shared BreakerRegistry (per provider/model).
    """

    def __init__(
        self,
        settings: Any,
        providers: dict[str, Any],
        limiter: ModelLimiter | None = None,
        breakers: BreakerRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._providers = dict(providers)
        self._limiter = limiter or ModelLimiter()
        self._breakers = breakers or get_breaker_registry()
        # Configure per-model limits and feed live rate headers back.
        for name, provider in self._providers.items():
            observer = getattr(provider, "set_rate_observer", None)
            if observer is not None:
                observer(
                    lambda model, info: self._limiter.observe_remaining(
                        model, getattr(info, "remaining_tokens", None) if info else None
                    )
                )
        self._local_sem: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}
        self._local_inflight = 0
        self._local_down_until = 0.0
        self._ewma_latency: dict[str, float] = {}
        self._slow_until: dict[str, float] = {}
        self._ewma_gen: dict[str, float] = {}
        for model in self._all_chain_models():
            resolved = self._resolve(model)
            if resolved is None:
                continue
            provider_name, _, _ = resolved
            limits = self._settings.model_limits(provider_name, model) if self._settings else {}
            self._limiter.configure(
                model,
                rpm=limits.get("rpm", 30),
                tpm=limits.get("tpm", 200_000),
                rpd=limits.get("rpd", 1_000),
            )

    # ── LLMPort ──────────────────────────────────────────────────────────────

    def available(self) -> bool:
        """True when at least one provider is configured."""
        return bool(self._providers)

    async def complete(self, spec: TaskSpec) -> LLMResult:
        """Plain or JSON completion through the tier chain."""
        return await self._complete(spec, exclude=frozenset())

    async def complete_json(self, spec: TaskSpec) -> LLMResult:
        """JSON-mode completion with lenient parsing; one retry on the next model."""
        spec = dataclasses.replace(spec, json_mode=True)
        result = await self._complete(spec, exclude=frozenset())
        try:
            result.parsed = parse_json_lenient(result.content)
            return result
        except SchemaViolationError:
            run_context.emit(
                "llm_router",
                f"JSON parse failed on {result.model} — retrying on next model",
                kind=EventKind.WARNING,
                severity=EventSeverity.WARNING,
            )
        result = await self._complete(spec, exclude=frozenset({result.model}))
        result.parsed = parse_json_lenient(result.content)
        return result

    async def aclose(self) -> None:
        """Close per-loop provider clients (call at end of run loop)."""
        for provider in self._providers.values():
            close = getattr(provider, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    logger.debug("provider aclose failed", exc_info=True)

    # ── Chain resolution ─────────────────────────────────────────────────────

    def _all_chain_models(self) -> list[str]:
        models: list[str] = []
        if self._settings is None:
            return models
        for fn in (self._settings.fast_models, self._settings.reasoning_models,
                   self._settings.vision_models):
            models.extend(fn())
        return models

    def _models_for(self, tier: LLMTier) -> list[str]:
        if self._settings is None:
            return []
        if tier == LLMTier.REASONING:
            return self._settings.reasoning_models()
        if tier == LLMTier.VISION:
            return self._settings.vision_models()
        return self._settings.fast_models()

    def _resolve(self, model: str) -> tuple[str, Any, str] | None:
        """Map a chain entry to ``(provider_name, provider, model_id)`` or None."""
        model_id = model
        forced: str | None = None
        for prefix in ("local:", "groq:", "gemini:", "openai:"):
            if model.startswith(prefix):
                forced = prefix[:-1]
                model_id = model[len(prefix):]
                break
        if forced:
            provider_name = forced
        elif model_id.startswith("gemini-"):
            provider_name = "gemini"
        elif "groq" in self._providers:
            provider_name = "groq"
        elif "openai" in self._providers:
            provider_name = "openai"
        else:
            return None
        provider = self._providers.get(provider_name)
        if provider is None:
            return None
        return provider_name, provider, model_id

    _QUALITY_PURPOSES = {"sop_generation", "alignment_judgement"}

    def _local_semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        sem = self._local_sem.get(loop)
        if sem is None:
            slots = int(getattr(self._settings, "local_llm_parallel", 3) or 3)
            sem = self._local_sem[loop] = asyncio.Semaphore(slots)
        return sem

    def _eta(self, provider_name: str, model: str, spec: TaskSpec,
             est_tokens: int) -> float:
        """Expected finish time (s); inf = excluded (breaker/down/context)."""
        breaker = self._breakers.get(provider_name, model)
        if breaker.is_open():
            return float("inf")
        if provider_name == "local":
            if time.monotonic() < self._local_down_until:
                return float("inf")
            max_in = int(getattr(self._settings,
                                 "local_llm_max_input_tokens", 4500) or 4500)
            if spec.estimated_input_tokens() > max_in:
                return float("inf")
            slots = int(getattr(self._settings, "local_llm_parallel", 3) or 3)
            queue_wait = max(0, self._local_inflight - slots + 1) * self._ewma_latency.get(model, 15.0)
            prefill = spec.estimated_input_tokens() / 500.0
            gen = spec.max_output_tokens / self._ewma_gen.get(model, 25.0)
            return queue_wait + prefill + gen
        wait = self._limiter.wait_time(model, est_tokens)
        if wait == float("inf"):
            return float("inf")
        slow = 30.0 if time.monotonic() < self._slow_until.get(model, 0.0) else 0.0
        return wait + self._ewma_latency.get(model, 8.0) + slow

    def _emit_skip(self, msg: str) -> None:
        logger.debug("router: %s", msg)
        run_context.emit(
            "llm_router", msg, kind=EventKind.WARNING,
            severity=EventSeverity.WARNING)

    @staticmethod
    def _NullSem():
        class _S:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return None
        return _S()

    # ── Core loop ────────────────────────────────────────────────────────────

    async def _complete(self, spec: TaskSpec, exclude: frozenset[str]) -> LLMResult:
        ctx = run_context.current()
        downshifted = False

        # Per-run budgets
        if ctx is not None:
            allowed, reasoning_ok = ctx.try_debit_llm(spec.tier == LLMTier.REASONING)
            if not allowed:
                raise LLMUnavailableError(
                    "router", "per-run LLM call budget exhausted", attempts=[]
                )
            if spec.tier == LLMTier.REASONING and not reasoning_ok:
                downshifted = True
                run_context.emit(
                    "llm_router",
                    "Reasoning-call budget exhausted — downshifting to fast chain",
                    kind=EventKind.WARNING,
                    severity=EventSeverity.WARNING,
                )
                spec = dataclasses.replace(spec, tier=LLMTier.FAST)

        chain = [
            resolved
            for resolved in (self._resolve(m) for m in self._models_for(spec.tier))
            if resolved is not None and resolved[2] not in exclude
        ]
        if not chain:
            raise LLMUnavailableError(
                "router",
                f"No configured providers serve tier {spec.tier.value}",
                attempts=[],
            )

        attempts: list[str] = []
        est_tokens = spec.estimated_total_tokens()
        deferred: tuple[str, Any, str] | None = None
        deferred_wait = float("inf")

        # ETA-scheduled order: lowest expected finish first. Quality floor:
        # high-stakes purposes prefer API while within 1.5×local+20 s.
        etas = {(pn, m): self._eta(pn, m, spec, est_tokens)
                for pn, _, m in chain}
        if spec.purpose in self._QUALITY_PURPOSES:
            api_etas = [e for (pn, m), e in etas.items()
                        if pn != "local" and e != float("inf")]
            loc_eta = min((e for (pn, m), e in etas.items() if pn == "local"),
                          default=float("inf"))
            if api_etas and min(api_etas) < 1.5 * loc_eta + 20.0:
                etas = {(pn, m): (e if pn != "local" else float("inf"))
                        for (pn, m), e in etas.items()}
        chain = sorted(chain, key=lambda t: etas.get((t[0], t[2]),
                                   float("inf")))
        if chain:
            run_context.emit(
                "llm_router",
                f"{spec.purpose or spec.tier.value} → {chain[0][2]} "
                f"(ETA {etas.get((chain[0][0], chain[0][2]), 0):.0f}s)")

        for i, (provider_name, provider, model) in enumerate(chain):
            breaker = self._breakers.get(provider_name, model)
            if breaker.is_open():
                attempts.append(f"{model}: breaker {breaker.state.value}")
                self._emit_skip(f"{model} skipped — breaker {breaker.state.value}")
                continue

            if provider_name == "local":
                wait = 0.0
            else:
                wait = self._limiter.wait_time(model, est_tokens)
            if wait == float("inf"):
                attempts.append(f"{model}: daily quota exhausted")
                self._emit_skip(f"{model} skipped — daily quota exhausted")
                continue
            if wait > _MAX_IMMEDIATE_WAIT_S:
                attempts.append(f"{model}: needs {wait:.0f}s")
                logger.debug("router: %s deferred — needs %.0fs for TPM budget", model, wait)
                if wait < deferred_wait:       # prefer the shortest wait overall
                    deferred_wait = wait
                    deferred = (provider_name, provider, model)
                continue

            # Up to two attempts on this model (one same-model retry).
            pause = 0.0
            for attempt_no in (0, 1):
                if pause:
                    await asyncio.sleep(pause)
                try:
                    return await self._attempt(
                        provider_name, provider, model, spec, est_tokens,
                        ctx, attempts, downshifted,
                    )
                except RateLimitedError as exc:
                    # Rate limits are budget, not failure — penalize the bucket,
                    # never the breaker.
                    self._limiter.penalize(model, exc.retry_after or 25.0)
                    attempts.append(f"{model}: rate-limited")
                    self._emit_failover(model, chain, i, "rate-limited")
                    if 0 < exc.retry_after <= _SAME_MODEL_RETRY_HINT_S and attempt_no == 0:
                        pause = exc.retry_after
                        continue
                    break
                except TransientError as exc:
                    breaker.record_failure(exc)
                    attempts.append(f"{model}: transient ({exc.message[:60]})")
                    self._emit_failover(model, chain, i, "transient error")
                    if attempt_no == 0 and "timed out" not in exc.message:
                        pause = 0.5 * (1.0 + random.uniform(-0.1, 0.1))
                        continue
                    break
                except ModelUnavailableError as exc:
                    # Permanent/config rejection (400 INVALID_ARGUMENT etc.) —
                    # logged, skipped, never counted by the breaker.
                    attempts.append(f"{model}: {type(exc).__name__}")
                    logger.warning("router: %s rejected (%s)", model, exc.message[:90])
                    break
                except AuthError as exc:
                    breaker.record_failure(exc)
                    attempts.append(f"{model}: {type(exc).__name__}")
                    break
                except ContextTooLongError as exc:
                    attempts.append(f"{model}: context too long")
                    breaker.record_failure(exc)
                    break
                except QuotaExhaustedError as exc:
                    breaker.record_failure(exc)
                    self._limiter.mark_day_exhausted(model)
                    attempts.append(f"{model}: quota exhausted")
                    break
                except AdapterError as exc:
                    attempts.append(f"{model}: {type(exc).__name__}: {exc.message[:60]}")
                    break

        # Every candidate needed a wait — sleep for the shortest one, then try it.
        if deferred is not None:
            budget = ctx.time_left() if ctx is not None else _MAX_DEFERRED_WAIT_S
            sleep_s = min(deferred_wait, budget, _MAX_DEFERRED_WAIT_S)
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)
            provider_name, provider, model = deferred
            try:
                return await self._attempt(
                    provider_name, provider, model, spec, est_tokens,
                    ctx, attempts, downshifted,
                )
            except AdapterError as exc:
                attempts.append(f"{model}: {type(exc).__name__}: {exc.message[:60]}")

        raise LLMUnavailableError(
            "router",
            f"All models failed for tier {spec.tier.value}",
            attempts=attempts,
        )

    async def _attempt(
        self,
        provider_name: str,
        provider: Any,
        model: str,
        spec: TaskSpec,
        est_tokens: int,
        ctx: Any,
        attempts: list[str],
        downshifted: bool,
    ) -> LLMResult:
        if provider_name != "local":
            self._limiter.reserve(model, est_tokens)
        t0 = time.monotonic()
        outcome = "success"
        result: LLMResult | None = None
        if provider_name == "local":
            timeout = min(120.0, max(15.0,
                2.0 * self._eta(provider_name, model, spec, est_tokens)))
        else:
            timeout = (min(_FAST_TIMEOUT_MAX_S, max(_FAST_TIMEOUT_MIN_S,
                                                    3.0 * self._ewma_latency.get(model, 8.0) + 4.0))
                       if spec.tier == LLMTier.FAST else 70.0)
        if ctx is not None:
            timeout = min(timeout, max(5.0, ctx.time_left() - 2.0))
        try:
            self._local_inflight += 1 if provider_name == "local" else 0
            sem = (self._local_semaphore() if provider_name == "local"
                   else self._NullSem())
            async with sem:
                result = await asyncio.wait_for(
                    provider.generate(model, spec), timeout=timeout
                )
            self._breakers.get(provider_name, model).record_success()
        except asyncio.TimeoutError as exc:
            outcome = "timeout"
            self._slow_until[model] = time.monotonic() + 90.0
            raise TransientError(provider_name, f"{model}: call timed out") from exc
        except TransientError as exc:
            outcome = "error"
            if provider_name == "local" and "unreachable" in exc.message.lower():
                self._local_down_until = time.monotonic() + 30.0
                run_context.emit(
                    "llm_router", "local server unreachable — marking down 30s",
                    kind=EventKind.WARNING, severity=EventSeverity.WARNING)
            raise
        except AdapterError:
            outcome = "error"
            raise
        finally:
            if provider_name == "local":
                self._local_inflight -= 1
            lat_s = time.monotonic() - t0
            self._ewma_latency[model] = (
                0.7 * self._ewma_latency.get(model, lat_s) + 0.3 * lat_s)
            if result is not None and result.usage.output_tokens > 0 and lat_s > 0:
                r = result.usage.output_tokens / lat_s
                self._ewma_gen[model] = (
                    0.7 * self._ewma_gen.get(model, r) + 0.3 * r)
            latency = (time.monotonic() - t0) * 1000.0
            if ctx is not None:
                ctx.record_usage(UsageRecord(
                    provider=provider_name,
                    model=model,
                    tier=spec.tier,
                    purpose=spec.purpose,
                    input_tokens=result.usage.input_tokens if result else est_tokens,
                    output_tokens=result.usage.output_tokens if result else 0,
                    reasoning_tokens=result.usage.reasoning_tokens if result else 0,
                    latency_ms=latency,
                    outcome=outcome,
                    downshifted=downshifted,
                ))
        assert result is not None
        result.provider = provider_name
        result.model = model
        result.tier = spec.tier
        result.usage.latency_ms = result.usage.latency_ms or (time.monotonic() - t0) * 1000.0
        result.attempts = attempts + [f"{model}: ok"]
        result.fallback_used = downshifted or bool(attempts)
        return result

    def _emit_failover(self, model: str, chain: list, index: int, reason: str) -> None:
        """Emit a WARNING event naming the model we are moving away from."""
        nxt = chain[index + 1][2] if index + 1 < len(chain) else "none"
        run_context.emit(
            "llm_router",
            f"LLM failover: {model} {reason} → {nxt}",
            kind=EventKind.WARNING,
            severity=EventSeverity.WARNING,
        )
