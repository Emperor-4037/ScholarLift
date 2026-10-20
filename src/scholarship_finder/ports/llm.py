"""LLM port contract — vendor-neutral capability gateway.

Agents request a capability tier (fast / reasoning / vision).
The router selects the first viable adapter from the configured chain.
No model ID or vendor type ever crosses this boundary into the core.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

from scholarship_finder.domain.models import LLMTier


@dataclass
class TaskSpec:
    """Provider-neutral task description passed to the LLM port.

    The adapter renders this into its vendor's message format.
    Never contains vendor-specific fields.
    """
    tier: LLMTier
    instruction: str                        # System-level instruction
    data_blocks: list[dict[str, str]] = field(default_factory=list)
    # Each block: {"label": "RESUME_TEXT", "content": "..."}
    output_schema: dict[str, Any] | None = None  # Portable Schema Profile JSON schema
    max_output_tokens: int = 4096
    reasoning_effort: str = "none"          # "none" | "low" | "medium" | "high"
    data_sensitive: bool = False            # True → only adapters with may_train=False
    temperature: float = 0.2


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: float = 0.0


@dataclass
class LLMResult:
    """Normalized result returned by any LLM adapter."""
    content: str                            # Raw text or JSON string
    parsed: Any | None = None              # Pydantic model if schema was provided
    usage: LLMUsage = field(default_factory=LLMUsage)
    provider: str = ""
    model: str = ""
    tier: LLMTier = LLMTier.FAST
    downshifted: bool = False              # True if a lower tier was used


class LLMPort(Protocol):
    """Narrow interface implemented by every LLM adapter."""

    async def complete(self, spec: TaskSpec) -> LLMResult:
        """Return a structured or plain-text completion."""
        ...

    async def stream(self, spec: TaskSpec) -> AsyncIterator[str]:
        """Yield text deltas; final chunk is empty string."""
        ...

    def capabilities(self) -> dict[str, Any]:
        """Return adapter capability flags.

        Expected keys:
          - native_json_schema: bool
          - streaming: bool
          - vision: bool
          - max_context_tokens: int
          - max_output_tokens: int
          - reasoning_control: bool
          - may_train: bool | None   (None = unknown)
          - data_policy: str
        """
        ...
