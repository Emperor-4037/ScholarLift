"""Google Gemini LLM provider — implements the LLMProvider protocol.

Used for every ``gemini-*`` model in the tier chains (fast, reasoning and
vision). The :class:`LLMRouter` picks the model; this adapter only knows
how to talk to the Gemini API.

- ``genai.Client`` instances are cached per running event loop (each run
  gets its own loop on a background thread).
- ``json_mode`` → ``response_mime_type="application/json"``.
- Images in ``TaskSpec.images`` are sent as ``Part.from_bytes`` parts.
- A modest thinking budget is set for the reasoning tier only.
- All SDK exceptions are mapped to the normalized error taxonomy.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from google import genai
from google.genai import types

from scholarship_finder.errors import (
    AdapterError,
    AuthError,
    ContextTooLongError,
    ModelUnavailableError,
    QuotaExhaustedError,
    RateLimitedError,
    RateLimitScope,
    TransientError,
    UnknownAdapterError,
)
from scholarship_finder.ports.llm import LLMResult, LLMUsage, RateInfo, TaskSpec

logger = logging.getLogger(__name__)

PROVIDER = "gemini"

# Thinking budget for reasoning-tier calls (gemini-2.5+/3 flash models).
_THINKING_HEADROOM = 1024


class GeminiProvider:
    """Gemini API endpoint for all ``gemini-*`` models.

    Args:
        api_key: Gemini API key (GEMINI_API_KEY).
        name:    Provider label used in results/telemetry.
    """

    def __init__(self, api_key: str, name: str = PROVIDER) -> None:
        self.name = name
        self._api_key = api_key
        # Clients are bound to the loop that created them — keep one per loop.
        self._clients: dict[asyncio.AbstractEventLoop, genai.Client] = {}
        self._rate_observer: Callable[[str, RateInfo | None], None] | None = None

    # ── Client lifecycle ─────────────────────────────────────────────────────

    def _client(self) -> genai.Client:
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = genai.Client(api_key=self._api_key)
            self._clients[loop] = client
        return client

    async def aclose(self) -> None:
        """Drop the client bound to the current loop (call at run end)."""
        loop = asyncio.get_running_loop()
        self._clients.pop(loop, None)

    def set_rate_observer(self, observer: Callable[[str, RateInfo | None], None]) -> None:
        """Register a callback ``(model, RateInfo|None)`` fed after each call."""
        self._rate_observer = observer

    # ── Generation ───────────────────────────────────────────────────────────

    async def generate(self, model: str, spec: TaskSpec) -> LLMResult:
        """One completion call; raises mapped AdapterError subclasses."""
        client = self._client()
        contents = self._build_contents(spec)
        config = self._build_config(spec)

        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            raise self._map_error(exc, model) from exc

        usage = LLMUsage()
        meta = getattr(response, "usage_metadata", None)
        if meta is not None:
            usage.input_tokens = getattr(meta, "prompt_token_count", 0) or 0
            usage.output_tokens = getattr(meta, "candidates_token_count", 0) or 0
            usage.reasoning_tokens = getattr(meta, "thoughts_token_count", 0) or 0

        text = getattr(response, "text", None) or ""
        if not text:
            # Fallback: read parts manually (some SDK versions leave .text empty
            # when the first candidate is a thought part).
            try:
                for cand in getattr(response, "candidates", None) or []:
                    for part in getattr(cand.content, "parts", None) or []:
                        t = getattr(part, "text", None)
                        if t:
                            text += t
            except Exception:
                pass
        if not text:
            raise TransientError(self.name, f"{model}: empty response")

        if self._rate_observer is not None:
            try:
                self._rate_observer(model, None)
            except Exception:
                logger.debug("rate observer failed", exc_info=True)

        return LLMResult(
            content=text,
            usage=usage,
            provider=self.name,
            model=model,
            tier=spec.tier,
        )

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _build_contents(self, spec: TaskSpec) -> Any:
        parts: list[Any] = []
        if spec.prompt:
            parts.append(spec.prompt)
        for img in spec.images:
            parts.append(types.Part.from_bytes(data=img, mime_type="image/png"))
        if not parts:
            parts.append("Begin.")
        return parts

    def _build_config(self, spec: TaskSpec) -> types.GenerateContentConfig:
        kwargs: dict[str, Any] = {
            "max_output_tokens": spec.max_output_tokens,
            "temperature": spec.temperature,
        }
        if spec.system:
            kwargs["system_instruction"] = spec.system
        if spec.json_mode:
            kwargs["response_mime_type"] = "application/json"
        # Gemini 3.x rejects thinking_budget=0 (400) and some models reject "minimal";
        # "low" is accepted everywhere and keeps thought from eating the output budget.
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="low")
        kwargs["max_output_tokens"] = spec.max_output_tokens + _THINKING_HEADROOM
        return types.GenerateContentConfig(**kwargs)

    def _map_error(self, exc: Exception, model: str) -> AdapterError:
        provider = self.name
        code = getattr(exc, "code", None)
        status = str(getattr(exc, "status", "") or "").upper()
        msg = str(exc)

        if code == 429 or "RESOURCE_EXHAUSTED" in status:
            # Gemini daily-quota exhaustion usually reports as RESOURCE_EXHAUSTED
            # with "quota" in the message; per-minute bursts are plain 429s.
            if "quota" in msg.lower() and "per_day" in msg.lower():
                return QuotaExhaustedError(provider, f"{model}: daily quota: {msg}")
            return RateLimitedError(
                provider, f"{model}: rate limited: {msg}",
                scope=RateLimitScope.MINUTE, retry_after=_retry_after(exc),
            )
        if code in (401, 403) or status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
            return AuthError(provider, f"{model}: auth/permission: {msg}")
        if code == 404 or status == "NOT_FOUND":
            return ModelUnavailableError(provider, f"{model}: not found: {msg}")
        if code == 400 or status == "INVALID_ARGUMENT":
            return ModelUnavailableError(provider, f"{model}: {msg}")
            low = msg.lower()
            if "context" in low or "token" in low and "exceed" in low or "too long" in low:
                return ContextTooLongError(provider, f"{model}: {msg}")
            return TransientError(provider, f"{model}: invalid request: {msg}")
        if code in (408, 504) or status in ("DEADLINE_EXCEEDED",):
            return TransientError(provider, f"{model}: deadline exceeded: {msg}")
        if code is not None and code >= 500 or status in ("UNAVAILABLE", "INTERNAL"):
            return TransientError(provider, f"{model}: server error {code}: {msg}")
        return UnknownAdapterError(provider, f"{model}: {msg}", exc)


def _retry_after(exc: Exception) -> float:
    """Best-effort retry-after seconds from a Gemini error's details."""
    details = getattr(exc, "details", None) or {}
    try:
        for item in details.get("error", {}).get("details", []):
            if item.get("@type", "").endswith("RetryInfo"):
                delay = item.get("retryDelay", "")
                return float(str(delay).rstrip("s"))
    except Exception:
        pass
    return 0.0
