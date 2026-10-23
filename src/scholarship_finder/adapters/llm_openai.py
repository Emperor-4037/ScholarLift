"""OpenAI-compatible LLM adapter — works with any OpenAI-style REST endpoint.

Supports: Groq, OpenRouter, GitHub Models, local Ollama, etc.
Configured entirely by base URL + model name + capability declaration in settings.
All vendor errors mapped to the normalized error taxonomy.
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator

from scholarship_finder.domain.models import LLMTier
from scholarship_finder.errors import (
    AuthError,
    ContentBlockedError,
    ContextTooLongError,
    ModelUnavailableError,
    QuotaExhaustedError,
    RateLimitedError,
    RateLimitScope,
    SchemaViolationError,
    TimeoutError as SFTimeoutError,
    TransientError,
    UnknownAdapterError,
)
from scholarship_finder.ports.llm import LLMResult, LLMUsage, TaskSpec

try:
    from openai import AsyncOpenAI, APIStatusError, APIConnectionError, APITimeoutError
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False

PROVIDER = "openai_compat"


def _map_error(exc: Exception, model: str) -> Exception:
    """Translate openai SDK exceptions → normalized error taxonomy."""
    if not _OPENAI_AVAILABLE:
        return UnknownAdapterError(PROVIDER, "openai not installed", exc)

    if isinstance(exc, APIStatusError):
        status = exc.status_code
        msg = str(exc.message)
        if status == 429:
            retry_after = 0.0
            if hasattr(exc, "response") and exc.response is not None:
                ra = exc.response.headers.get("retry-after", "0")
                try:
                    retry_after = float(ra)
                except ValueError:
                    pass
            if retry_after > 60:
                return QuotaExhaustedError(PROVIDER, f"Daily quota exhausted for {model}")
            return RateLimitedError(
                PROVIDER, f"Rate limited: {msg}", RateLimitScope.MINUTE, retry_after
            )
        if status in (401, 403):
            return AuthError(PROVIDER, f"Auth failed: {msg}")
        if status == 404:
            return ModelUnavailableError(PROVIDER, f"Model {model} not found: {msg}")
        if status in (500, 502, 503):
            return TransientError(PROVIDER, f"Server error {status}: {msg}")
        if "context" in msg.lower() and ("length" in msg.lower() or "token" in msg.lower()):
            return ContextTooLongError(PROVIDER, f"Context too long for {model}: {msg}")
        if "content_filter" in msg.lower() or "safety" in msg.lower():
            return ContentBlockedError(PROVIDER, f"Content blocked: {msg}")

    if isinstance(exc, APITimeoutError):
        return SFTimeoutError(PROVIDER, f"Timeout on {model}")

    if isinstance(exc, APIConnectionError):
        return TransientError(PROVIDER, f"Connection error: {exc}")

    return UnknownAdapterError(PROVIDER, f"Unexpected: {exc}", exc)


class OpenAICompatAdapter:
    """Adapter for any OpenAI-compatible endpoint.

    Args:
        api_key:      Bearer token / API key.
        base_url:     Base URL of the compatible endpoint (e.g. https://api.groq.com/openai/v1).
        model_id:     Model name as expected by the endpoint.
        tier:         Logical tier this adapter serves.
        may_train:    Whether the provider may use prompts for training.
        supports_json_schema: Whether the endpoint supports strict JSON schema output.
        bus:          Optional EventBus for error reporting.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        tier: LLMTier,
        may_train: bool = False,
        supports_json_schema: bool = True,
        bus=None,
    ) -> None:
        if not _OPENAI_AVAILABLE:
            raise ImportError("openai package not installed. Run: pip install openai")
        self._model_id = model_id
        self._tier = tier
        self._may_train = may_train
        self._supports_json = supports_json_schema
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url or None)
        self._bus = bus  # Optional EventBus for error reporting

    async def complete(self, spec: TaskSpec) -> LLMResult:
        messages = self._build_messages(spec)
        kwargs: dict[str, Any] = {
            "model": self._model_id,
            "messages": messages,
            "max_tokens": spec.max_output_tokens,
            "temperature": spec.temperature,
        }
        if spec.output_schema and self._supports_json:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "strict": True,
                    "schema": spec.output_schema,
                },
            }
        t0 = time.monotonic()
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            mapped_error = _map_error(exc, self._model_id)
            # Publish error event if EventBus is available
            if self._bus:
                from scholarship_finder.domain.models import Event, EventKind, EventSeverity
                from datetime import datetime, timezone
                try:
                    await self._bus.publish(Event(
                        run_id=spec.data_blocks[0].get("run_id", "unknown") if spec.data_blocks else "unknown",
                        seq=0,
                        timestamp=datetime.now(timezone.utc),
                        node="llm_openai",
                        severity=EventSeverity.ERROR,
                        kind=EventKind.ERROR,
                        payload={
                            "model": self._model_id,
                            "error_type": type(mapped_error).__name__,
                            "message": str(mapped_error),
                        }
                    ))
                except Exception:
                    pass  # Don't let event publishing fail the LLM call
            raise mapped_error from exc

        latency_ms = (time.monotonic() - t0) * 1000
        return self._parse_response(response, spec, latency_ms)

    async def stream(self, spec: TaskSpec) -> AsyncIterator[str]:
        messages = self._build_messages(spec)
        try:
            stream = self._client.chat.completions.create(
                model=self._model_id,
                messages=messages,
                max_tokens=spec.max_output_tokens,
                temperature=spec.temperature,
                stream=True,
            )
            async for chunk in stream:
                try:
                    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                        yield chunk.choices[0].delta.content
                except (AttributeError, IndexError):
                    pass
        except Exception as exc:
            raise _map_error(exc, self._model_id) from exc
        yield ""  # sentinel

    def capabilities(self) -> dict[str, Any]:
        return {
            "native_json_schema": self._supports_json,
            "streaming": True,
            "vision": False,
            "max_context_tokens": 128_000,
            "max_output_tokens": 8192,
            "reasoning_control": False,
            "may_train": self._may_train,
            "data_policy": "openai_compatible",
            "provider": PROVIDER,
            "model": self._model_id,
            "tier": self._tier.value,
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_messages(self, spec: TaskSpec) -> list[dict[str, str]]:
        user_parts = []
        for block in spec.data_blocks:
            label = block.get("label", "DATA")
            content = block.get("content", "")
            user_parts.append(f"--- {label} ---\n{content}\n--- END {label} ---")
        return [
            {"role": "system", "content": spec.instruction},
            {"role": "user", "content": "\n\n".join(user_parts) if user_parts else "Begin."},
        ]

    def _parse_response(self, response: Any, spec: TaskSpec, latency_ms: float) -> LLMResult:
        choice = response.choices[0] if response.choices else None
        text = (choice.message.content or "") if choice else ""
        usage_obj = getattr(response, "usage", None)
        usage = LLMUsage(
            input_tokens=getattr(usage_obj, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage_obj, "completion_tokens", 0) or 0,
            latency_ms=latency_ms,
        )
        parsed = None
        if spec.output_schema and text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as e:
                raise SchemaViolationError(PROVIDER, f"JSON parse failed: {e}", raw_output=text) from e
        return LLMResult(
            content=text, parsed=parsed, usage=usage,
            provider=PROVIDER, model=self._model_id, tier=self._tier,
        )


class OpenAILLMAdapter:
    """Multi-tier LLM adapter wrapping OpenAI-compatible endpoints (Groq, OpenRouter, etc.)."""

    def __init__(self, api_key: str, settings: Any = None, bus=None) -> None:
        self._api_key = api_key
        self._settings = settings
        self._bus = bus  # Optional EventBus for error reporting
        base_url = (
            getattr(settings, "groq_api_base_url", "")
            or getattr(settings, "openai_api_base_url", "")
            or ""
        ).strip()
        if not base_url and (
            getattr(settings, "groq_api_key", "")
            or api_key.startswith("gsk_")
        ):
            base_url = "https://api.groq.com/openai/v1"
        self._base_url = base_url.rstrip("/") if base_url else ""
        self._adapters: dict[str, OpenAICompatAdapter] = {}

    def _get_adapter(self, tier: LLMTier) -> OpenAICompatAdapter:
        model = None
        if self._settings:
            if tier == LLMTier.REASONING:
                models = [m for m in self._settings.reasoning_models() if not m.startswith("gemini")]
                if models:
                    model = models[0]
            elif tier == LLMTier.VISION:
                models = [m for m in self._settings.vision_models() if not m.startswith("gemini")]
                if models:
                    model = models[0]
            else:
                models = [m for m in self._settings.fast_models() if not m.startswith("gemini")]
                if models:
                    model = models[0]

        if not model:
            if "groq.com" in self._base_url:
                model = "openai/gpt-oss-120b" if tier == LLMTier.REASONING else "openai/gpt-oss-20b"
            else:
                model = "gpt-4o" if tier == LLMTier.REASONING else "gpt-4o-mini"

        cache_key = f"{model}:{tier.value}"
        if cache_key not in self._adapters:
            self._adapters[cache_key] = OpenAICompatAdapter(
                api_key=self._api_key,
                base_url=self._base_url,
                model_id=model,
                tier=tier,
                bus=self._bus,
            )
        return self._adapters[cache_key]

    async def complete(self, spec: TaskSpec) -> LLMResult:
        adapter = self._get_adapter(spec.tier)
        return await adapter.complete(spec)

    async def stream(self, spec: TaskSpec) -> AsyncIterator[str]:
        adapter = self._get_adapter(spec.tier)
        async for chunk in adapter.stream(spec):
            yield chunk

    def capabilities(self) -> dict[str, Any]:
        return {
            "native_json_schema": True,
            "streaming": True,
            "vision": False,
            "max_context_tokens": 128_000,
            "max_output_tokens": 8192,
            "reasoning_control": False,
            "may_train": False,
            "data_policy": "openai_compatible",
            "provider": PROVIDER,
        }

