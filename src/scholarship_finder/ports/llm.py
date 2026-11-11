"""LLM port contract — vendor-neutral capability gateway.

Agents request a capability tier (fast / reasoning / vision); the router picks
the model. No model ID or vendor type ever crosses this boundary into agents.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from scholarship_finder.domain.models import LLMTier


@dataclass
class TaskSpec:
    """Provider-neutral task description."""
    tier: LLMTier
    system: str                              # instructions (system message)
    prompt: str = "Begin."                   # task data (user message)
    json_mode: bool = False                  # ask for a JSON object / array response
    max_output_tokens: int = 2048
    temperature: float = 0.2
    reasoning_effort: str = "low"            # "low" | "medium" | "high" (where supported)
    purpose: str = ""                        # telemetry label, e.g. "profile_extraction"
    images: list[bytes] = field(default_factory=list)   # PNG bytes for vision tasks
    json_schema: dict | None = None  # schema-constrained JSON (local llama.cpp)

    def estimated_input_tokens(self) -> int:
        return int((len(self.system) + len(self.prompt)) / 3.5) + 50 + 1000 * len(self.images)

    def estimated_total_tokens(self) -> int:
        return self.estimated_input_tokens() + self.max_output_tokens


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: float = 0.0


@dataclass
class RateInfo:
    """Provider rate-limit headers, when exposed (Groq/OpenAI)."""
    remaining_tokens: int | None = None
    reset_tokens_s: float | None = None
    remaining_requests: int | None = None


@dataclass
class LLMResult:
    """Normalized result returned by the router."""
    content: str
    parsed: Any | None = None              # parsed JSON when json_mode=True
    usage: LLMUsage = field(default_factory=LLMUsage)
    provider: str = ""
    model: str = ""
    tier: LLMTier = LLMTier.FAST
    fallback_used: bool = False
    attempts: list[str] = field(default_factory=list)
    rate: RateInfo | None = None


class LLMProvider(Protocol):
    """One vendor endpoint; generates with an explicit model ID."""

    name: str

    async def generate(self, model: str, spec: TaskSpec) -> LLMResult:
        ...


class LLMPort(Protocol):
    """Interface agents depend on."""

    async def complete(self, spec: TaskSpec) -> LLMResult:
        """Plain or JSON completion (parsed result in ``.parsed`` when json_mode)."""
        ...

    def available(self) -> bool:
        """True when at least one provider is configured."""
        ...
