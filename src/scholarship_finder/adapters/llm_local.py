"""Local llama.cpp OpenAI-compatible provider (LocalLlamaProvider).

- Points at LOCAL_LLM_BASE_URL (default http://127.0.0.1:8088/v1).
- Schema-constrained JSON when TaskSpec.json_schema is set (llama.cpp
  accepts response_format={"type":"json_object","schema":{...}}); plain
  json_object otherwise.
- No reasoning_effort, no rate-limit headers, generous call budget —
  timeouts are owned by the router's ETA scheduler.
"""
from __future__ import annotations

import logging
from typing import Any

from scholarship_finder.adapters.llm_openai import OpenAICompatProvider
from scholarship_finder.errors import TransientError
from scholarship_finder.ports.llm import LLMResult, TaskSpec

logger = logging.getLogger(__name__)

PROVIDER = "local"


class LocalLlamaProvider(OpenAICompatProvider):
    """OpenAI-compat provider for a local llama.cpp server."""

    def __init__(self, api_key: str = "local", base_url: str = "http://127.0.0.1:8088/v1") -> None:
        super().__init__(name=PROVIDER, api_key=api_key, base_url=base_url)

    async def generate(self, model: str, spec: TaskSpec) -> LLMResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._build_messages(spec),
            "max_tokens": spec.max_output_tokens,
            "temperature": spec.temperature,
        }
        if spec.json_mode:
            rf: dict[str, Any] = {"type": "json_object"}
            if spec.json_schema:
                rf["schema"] = spec.json_schema
            kwargs["response_format"] = rf
        try:
            completion, _rate = await self._call(model, kwargs)
        except Exception as exc:
            err = self._map_error(exc, model)
            # Connection problems → transient so router marks local-down.
            msg = (str(exc) + getattr(err, "message", "")).lower()
            if "connect" in msg or "refused" in msg or "unreachable" in msg:
                raise TransientError(
                    PROVIDER, f"{model}: local server unreachable") from exc
            raise err

        content = ""
        finish = ""
        if getattr(completion, "choices", None):
            content = completion.choices[0].message.content or ""
            finish = getattr(completion.choices[0], "finish_reason", "") or ""
        if not content:
            raise TransientError(
                PROVIDER, f"{model}: empty content (finish_reason={finish or '?'})")

        usage = getattr(completion, "usage", None)
        from scholarship_finder.ports.llm import LLMUsage
        u = LLMUsage()
        if usage is not None:
            u.input_tokens = getattr(usage, "prompt_tokens", 0) or 0
            u.output_tokens = getattr(usage, "completion_tokens", 0) or 0
        return LLMResult(
            content=content, usage=u, provider=PROVIDER,
            model=model, tier=spec.tier,
        )
