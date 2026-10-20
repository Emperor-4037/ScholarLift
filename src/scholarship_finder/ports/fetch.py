"""Fetch port contract — tiered URL content retrieval gateway.

Fetch is a chain of responsibility: each tier either returns a FetchedDocument
or raises a classified failure that triggers escalation to the next tier.

Tiers (architecture §4.4):
  0 — In-memory TTL cache
  1 — Jina Reader (hosted, keyed)
  2 — Direct HTTP + local main-content extraction (trafilatura)
  3 — Headless browser (FULL profile only)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


@dataclass
class FetchPolicy:
    """Caller hints to guide fetch-tier selection."""
    timeout_s: float = 14.0           # hard cap per URL across all tiers
    max_bytes: int = 500_000
    js_rendering: str = "never"       # "never" | "if-needed" | "required"
    allow_cache: bool = True
    follow_redirects: bool = True
    max_redirects: int = 5


@dataclass
class FetchedDocument:
    """Normalized document returned by any fetch tier."""
    url: str                          # original URL requested
    final_url: str                    # URL after redirects
    status_class: str                 # "2xx" | "3xx" | "4xx" | "5xx" | "error"
    normalized_text: str              # whitespace-normalized markdown or plain text
    content_type: str = "text/html"
    title: str = ""
    tier_used: int = 1               # 0=cache, 1=jina, 2=direct, 3=browser
    truncated: bool = False
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    content_hash: str = ""           # SHA-256 of normalized_text for cache keying


class FetchPort(Protocol):
    """Narrow interface for the tiered fetch chain."""

    async def fetch(self, url: str, policy: FetchPolicy | None = None) -> FetchedDocument:
        """Fetch *url* respecting *policy*, escalating tiers as needed."""
        ...

    def capabilities(self) -> dict[str, Any]:
        """Return capability flags.

        Expected keys:
          - max_tier: int   (highest tier available; 2 for LITE, 3 for FULL)
          - handles_pdf: bool
          - handles_js: bool
        """
        ...
