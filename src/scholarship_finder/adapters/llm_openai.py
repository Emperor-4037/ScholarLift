"""OpenAI-compatible LLM provider — implements the LLMProvider protocol.

Used for Groq (base_url=https://api.groq.com/openai/v1) and any other
OpenAI-compatible endpoint. The :class:`LLMRouter` picks the model; this
adapter only knows how to talk to one vendor endpoint.

- ``AsyncOpenAI`` clients are cached per running event loop (each run gets
  its own loop on a background thread, so a shared client breaks on the
  second run).
- ``json_mode`` → ``response_format={"type": "json_object"}``.
- ``reasoning_effort`` is sent where supported; on a 400 rejection the
  call is retried once without it.
- Rate-limit headers (``x-ratelimit-remaining-tokens`` /
  ``x-ratelimit-reset-tokens``) are reported to the router's limiter via
  :class:`~scholarship_finder.ports.llm.RateInfo`.
- All SDK exceptions are mapped to the normalized error taxonomy.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Callable

import openai
from openai import AsyncOpenAI

from scholarship_finder.errors import (
    AdapterError,
    AuthError,
    ContextTooLongError,
    ModelUnavailableError,
    RateLimitedError,
    RateLimitScope,
    TransientError,
    UnknownAdapterError,
)
from scholarship_finder.ports.llm import LLMResult, LLMUsage, RateInfo, TaskSpec

logger = logging.getLogger(__name__)

_RATE_HEADER_REMAINING = "x-ratelimit-remaining-tokens"
_RATE_HEADER_RESET = "x-ratelimit-reset-tokens"
_RATE_HEADER_REMAINING_REQ = "x-ratelimit-remaining-requests"


def _parse_reset_seconds(value: str | None) -> float | None:
    """Parse Groq-style reset headers ("6s", "1m30s", "500ms") into seconds."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    total = 0.0
    num = ""
    for ch in value.strip():
        if ch.isdigit() or ch == ".":
            num += ch
        else:
            try:
                part = float(num) if num else 0.0
            except ValueError:
                part = 0.0
            num = ""
            if ch == "m" and "s" not in value[value.index(ch):]:
                total += part * 60.0
            elif ch == "h":
                total += part * 3600.0
            elif ch == "s":
                total += part
    return total or None


class OpenAICompatProvider:
    """One OpenAI-compatible endpoint (Groq, OpenAI, Together, ...).

    Args:
        name:     Provider label used in results/telemetry ("groq", "openai").
        api_key:  API key for the endpoint.
        base_url: Endpoint base URL ("" → SDK default, OpenAI only).
    """

    def __init__(self, name: str, api_key: str, base_url: str = "") -> None:
        self.name = name
        self._api_key = api_key
        self._base_url = base_url or None
        # Clients are bound to the loop that created them — keep one per loop.
        self._clients: dict[asyncio.AbstractEventLoop, AsyncOpenAI] = {}
        self._rate_observer: Callable[[str, RateInfo | None], None] | None = None

    # ── Client lifecycle ─────────────────────────────────────────────────────

    def _client(self) -> AsyncOpenAI:
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            kwargs: dict[str, Any] = {"api_key": self._api_key}
            if self._base_url:
                kwargs["base_url"] = self._base_url
            client = AsyncOpenAI(**kwargs)
            self._clients[loop] = client
        return client

    async def aclose(self) -> None:
        """Close the client bound to the current loop (call at run end)."""
        loop = asyncio.get_running_loop()
        client = self._clients.pop(loop, None)
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("OpenAICompatProvider: client close failed", exc_info=True)

    def set_rate_observer(self, observer: Callable[[str, RateInfo | None], None]) -> None:
        """Register a callback ``(model, RateInfo|None)`` fed after each call."""
        self._rate_observer = observer

    # ── Generation ───────────────────────────────────────────────────────────

    async def generate(self, model: str, spec: TaskSpec) -> LLMResult:
        """One completion call; raises mapped AdapterError subclasses."""
        messages = self._build_messages(spec)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": spec.max_output_tokens,
            "temperature": spec.temperature,
        }
        if spec.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if spec.reasoning_effort:
            kwargs["reasoning_effort"] = spec.reasoning_effort
        if "gpt-oss" in model.lower():
            # gpt-oss spends reasoning tokens out of max_tokens — headroom so
            # visible content isn't starved even at low effort.
            kwargs["max_tokens"] = spec.max_output_tokens + 1500

        try:
            completion, rate = await self._call(model, kwargs)
        except openai.BadRequestError as exc:
            # Some endpoints reject reasoning_effort — drop it once and retry.
            if "reasoning_effort" in kwargs and self._is_param_rejection(exc):
                kwargs.pop("reasoning_effort", None)
                try:
                    completion, rate = await self._call(model, kwargs)
                except Exception as exc2:
                    raise self._map_error(exc2, model) from exc2
            else:
                raise self._map_error(exc, model) from exc
        except Exception as exc:
            if isinstance(exc, AdapterError):
                raise
            raise self._map_error(exc, model) from exc

        usage = LLMUsage()
        if completion.usage is not None:
            usage.input_tokens = completion.usage.prompt_tokens or 0
            usage.output_tokens = completion.usage.completion_tokens or 0
            details = getattr(completion.usage, "completion_tokens_details", None)
            usage.reasoning_tokens = getattr(details, "reasoning_tokens", 0) or 0

        content = ""
        finish = ""
        if completion.choices:
            content = completion.choices[0].message.content or ""
            finish = getattr(completion.choices[0], "finish_reason", "") or ""
        if not content:
            raise TransientError(
                self.name,
                f"{model}: empty content (finish_reason={finish or '?'})")

        return LLMResult(
            content=content,
            usage=usage,
            provider=self.name,
            model=model,
            tier=spec.tier,
            rate=rate,
        )

    async def _call(self, model: str, kwargs: dict[str, Any]) -> tuple[Any, RateInfo]:
        client = self._client()
        try:
            raw = await client.chat.completions.with_raw_response.create(**kwargs)
        except openai.APIStatusError as exc:
            raise self._map_error(exc, model) from exc
        except openai.APIConnectionError as exc:
            raise TransientError(self.name, f"{model}: connection error: {exc}") from exc
        except openai.APITimeoutError as exc:
            raise TransientError(self.name, f"{model}: request timeout: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise TransientError(self.name, f"{model}: request timeout") from exc
        except Exception as exc:
            raise UnknownAdapterError(self.name, f"{model}: {exc}", exc) from exc

        completion = raw.parse()
        rate = RateInfo(
            remaining_tokens=_int_or_none(raw.headers.get(_RATE_HEADER_REMAINING)),
            reset_tokens_s=_parse_reset_seconds(raw.headers.get(_RATE_HEADER_RESET)),
            remaining_requests=_int_or_none(raw.headers.get(_RATE_HEADER_REMAINING_REQ)),
        )
        if self._rate_observer is not None:
            try:
                self._rate_observer(model, rate)
            except Exception:
                logger.debug("rate observer failed", exc_info=True)
        return completion, rate

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _build_messages(self, spec: TaskSpec) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if spec.system:
            messages.append({"role": "system", "content": spec.system})
        if spec.images:
            content: list[dict[str, Any]] = [{"type": "text", "text": spec.prompt or "Describe this image."}]
            for img in spec.images:
                b64 = base64.b64encode(img).decode("ascii")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                })
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": spec.prompt or "Begin."})
        return messages

    @staticmethod
    def _is_param_rejection(exc: openai.BadRequestError) -> bool:
        msg = str(exc).lower()
        return "reasoning_effort" in msg or "unsupported parameter" in msg or "unknown parameter" in msg

    def _map_error(self, exc: Exception, model: str) -> AdapterError:
        provider = self.name
        if isinstance(exc, openai.RateLimitError):
            retry_after = 0.0
            headers = getattr(exc.response, "headers", None)
            if headers is not None:
                retry_after = _parse_reset_seconds(headers.get("retry-after")) or 0.0
            return RateLimitedError(
                provider, f"{model}: rate limited: {exc}",
                scope=RateLimitScope.MINUTE, retry_after=retry_after,
            )
        if isinstance(exc, openai.AuthenticationError):
            return AuthError(provider, f"{model}: authentication failed: {exc}")
        if isinstance(exc, openai.PermissionDeniedError):
            return AuthError(provider, f"{model}: permission denied: {exc}")
        if isinstance(exc, openai.NotFoundError):
            return ModelUnavailableError(provider, f"{model}: not found: {exc}")
        if isinstance(exc, openai.BadRequestError):
            if _looks_like_context_error(exc):
                return ContextTooLongError(provider, f"{model}: context too long: {exc}")
            return TransientError(provider, f"{model}: bad request: {exc}")
        if isinstance(exc, openai.InternalServerError):
            return TransientError(provider, f"{model}: server error: {exc}")
        if isinstance(exc, openai.APIStatusError):
            if exc.status_code >= 500:
                return TransientError(provider, f"{model}: server error {exc.status_code}")
            if exc.status_code == 413:
                return ContextTooLongError(provider, f"{model}: payload too large")
            return TransientError(provider, f"{model}: HTTP {exc.status_code}: {exc}")
        if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
            return TransientError(provider, f"{model}: connection/timeout: {exc}")
        return UnknownAdapterError(provider, f"{model}: {exc}", exc)
def _looks_like_context_error(exc: openai.BadRequestError) -> bool:
    msg = str(exc).lower()
    code = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") or {}
        code = str(err.get("code", "")).lower()
    return (
        "context_length" in msg or "context length" in msg or "context_length" in code
        or "maximum context" in msg or "too many tokens" in msg or "reduce the length" in msg
    )


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


