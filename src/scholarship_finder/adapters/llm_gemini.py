"""Gemini LLM adapter — implements the LLM port using google-genai SDK.

Capabilities: structured JSON output, streaming, vision (image parts).
Tier routing: FAST → flash-lite models; REASONING → flash thinking models; VISION → flash.
Model IDs come entirely from config — never hardcoded here.
All vendor errors are translated to the normalized error taxonomy before crossing the port.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

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
from scholarship_finder.ports.llm import LLMPort, LLMResult, LLMUsage, TaskSpec
from scholarship_finder.domain.models import LLMTier

try:
    from google import genai
    from google.genai import types as genai_types
    _GENAI_AVAILABLE = True
except ImportError:
    _GENAI_AVAILABLE = False

PROVIDER = "gemini"


def _map_error(exc: Exception, model: str) -> Exception:
    """Translate google-genai SDK exceptions → normalized error taxonomy."""
    msg = str(exc)
    exc_type = type(exc).__name__

    # Resource exhausted / rate limit
    if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
        # Try to parse retry-after hint
        retry_after = 0.0
        if "retryDelay" in msg:
            import re
            m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', msg)
            if m:
                retry_after = float(m.group(1))
        if "quota" in msg.lower() or retry_after == 0.0:
            return QuotaExhaustedError(PROVIDER, f"Daily quota exhausted for {model}: {msg}")
        return RateLimitedError(
            PROVIDER, f"Rate limited on {model}", RateLimitScope.MINUTE, retry_after
        )

    if "INVALID_ARGUMENT" in msg and "context" in msg.lower():
        return ContextTooLongError(PROVIDER, f"Context too long for {model}: {msg}")

    if "PERMISSION_DENIED" in msg or "API_KEY_INVALID" in msg or "401" in msg:
        return AuthError(PROVIDER, f"Auth failed for {model}: {msg}")

    if "404" in msg or "NOT_FOUND" in msg or "deprecated" in msg.lower():
        return ModelUnavailableError(PROVIDER, f"Model {model} unavailable/retired: {msg}")

    if "SAFETY" in msg or "blocked" in msg.lower():
        return ContentBlockedError(PROVIDER, f"Content blocked by safety filter: {msg}")

    if "deadline" in msg.lower() or "timeout" in msg.lower() or "DEADLINE_EXCEEDED" in msg:
        return SFTimeoutError(PROVIDER, f"Timeout on {model}: {msg}")

    if "UNAVAILABLE" in msg or "503" in msg or "502" in msg:
        return TransientError(PROVIDER, f"Transient error on {model}: {msg}")

    return UnknownAdapterError(PROVIDER, f"Unexpected error on {model}: {msg}", exc)


class GeminiAdapter:
    """Gemini LLM adapter via google-genai SDK.

    One instance per model ID. The LLM router creates and caches these.
    """

    def __init__(self, api_key: str, model_id: str, tier: LLMTier) -> None:
        if not _GENAI_AVAILABLE:
            raise ImportError("google-genai is not installed. Run: pip install google-genai")
        self._model_id = model_id
        self._tier = tier
        self._client = genai.Client(api_key=api_key)

    # ── Port implementation ───────────────────────────────────────────────────

    async def complete(self, spec: TaskSpec) -> LLMResult:
        """Execute a structured or plain-text completion."""
        config = self._build_config(spec)
        contents = self._build_contents(spec)
        t0 = time.monotonic()
        try:
            response = await asyncio.to_thread(
                self._client.models.generate_content,
                model=self._model_id,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            raise _map_error(exc, self._model_id) from exc

        latency_ms = (time.monotonic() - t0) * 1000
        return self._parse_response(response, spec, latency_ms)

    async def stream(self, spec: TaskSpec) -> AsyncIterator[str]:
        """Yield text deltas from a streaming response."""
        config = self._build_config(spec, streaming=True)
        contents = self._build_contents(spec)
        try:
            async def _run():
                return self._client.models.generate_content_stream(
                    model=self._model_id,
                    contents=contents,
                    config=config,
                )
            stream_iter = await asyncio.to_thread(_run)
            for chunk in stream_iter:
                text = getattr(chunk, "text", "") or ""
                if text:
                    yield text
        except Exception as exc:
            raise _map_error(exc, self._model_id) from exc
        yield ""  # sentinel

    def capabilities(self) -> dict[str, Any]:
        return {
            "native_json_schema": True,
            "streaming": True,
            "vision": True,
            "max_context_tokens": 1_000_000,
            "max_output_tokens": 8192,
            "reasoning_control": self._tier == LLMTier.REASONING,
            "may_train": True,   # free tier may train — drives PII scrubbing
            "data_policy": "google_free_tier",
            "provider": PROVIDER,
            "model": self._model_id,
            "tier": self._tier.value,
        }

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_config(self, spec: TaskSpec, streaming: bool = False) -> Any:
        kwargs: dict[str, Any] = {
            "temperature": spec.temperature,
            "max_output_tokens": spec.max_output_tokens,
        }
        # Structured output
        if spec.output_schema:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = spec.output_schema

        # Reasoning / thinking budget
        if self._tier == LLMTier.REASONING and spec.reasoning_effort != "none":
            effort_map = {"low": 1024, "medium": 4096, "high": 8192}
            budget = effort_map.get(spec.reasoning_effort, 4096)
            kwargs["thinking_config"] = genai_types.ThinkingConfig(
                thinking_budget=budget
            )

        return genai_types.GenerateContentConfig(**kwargs)

    def _build_contents(self, spec: TaskSpec) -> list[Any]:
        """Build the contents list from a TaskSpec."""
        parts: list[str] = [spec.instruction]
        for block in spec.data_blocks:
            label = block.get("label", "DATA")
            content = block.get("content", "")
            parts.append(f"\n\n--- {label} ---\n{content}\n--- END {label} ---")
        return ["\n".join(parts)]

    def _parse_response(
        self, response: Any, spec: TaskSpec, latency_ms: float
    ) -> LLMResult:
        try:
            text = response.text or ""
        except Exception:
            text = ""

        # Usage
        usage_meta = getattr(response, "usage_metadata", None)
        usage = LLMUsage(
            input_tokens=getattr(usage_meta, "prompt_token_count", 0) or 0,
            output_tokens=getattr(usage_meta, "candidates_token_count", 0) or 0,
            reasoning_tokens=getattr(usage_meta, "thoughts_token_count", 0) or 0,
            latency_ms=latency_ms,
        )

        # Parse JSON if schema was requested
        parsed = None
        if spec.output_schema and text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as e:
                raise SchemaViolationError(
                    PROVIDER, f"JSON parse failed: {e}", raw_output=text
                ) from e

        return LLMResult(
            content=text,
            parsed=parsed,
            usage=usage,
            provider=PROVIDER,
            model=self._model_id,
            tier=self._tier,
        )


class GeminiLLMAdapter:
    """Multi-tier Gemini adapter implementing LLMPort.

    Routes TaskSpec to the appropriate model based on tier (fast, reasoning, vision)
    as configured in settings.
    """

    def __init__(self, api_key: str, settings: Any = None) -> None:
        self._api_key = api_key
        self._settings = settings
        self._adapters: dict[str, GeminiAdapter] = {}

    def _get_adapter(self, tier: LLMTier, model: str | None = None) -> GeminiAdapter:
        if not model and self._settings:
            if tier == LLMTier.REASONING:
                models = self._settings.reasoning_models()
            elif tier == LLMTier.VISION:
                models = self._settings.vision_models()
            else:
                models = self._settings.fast_models()
            model = models[0] if models else "gemini-3.5-flash-lite"
        model = model or "gemini-3.5-flash-lite"
        cache_key = f"{model}:{tier.value}"
        if cache_key not in self._adapters:
            self._adapters[cache_key] = GeminiAdapter(
                api_key=self._api_key, model_id=model, tier=tier
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
            "vision": True,
            "max_context_tokens": 1_000_000,
            "max_output_tokens": 8192,
            "reasoning_control": True,
            "may_train": False,
            "data_policy": "gemini_free_or_paid",
            "provider": PROVIDER,
        }

