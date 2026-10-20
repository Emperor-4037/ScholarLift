"""Search port contract — vendor-neutral web search gateway.

Adapters absorb Tavily, Jina Search, Serper, etc.
Result scores are not compared across providers; ranking uses domain trust.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class SearchQuery:
    """Normalized search request; adapters map this to their vendor format."""
    text: str
    intent: str = "program"               # "program" | "scholarship" | "funder"
    country: str | None = None
    include_domains: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=list)
    time_range: str | None = None         # e.g. "y" (past year), "m" (past month)
    depth: str = "basic"                  # "basic" (1 credit) | "advanced" (2 credits)
    max_results: int = 10


@dataclass
class SearchResultItem:
    url: str
    title: str
    snippet: str
    published_date: str | None = None
    page_content: str | None = None       # if the provider returns full content


@dataclass
class SearchResult:
    items: list[SearchResultItem] = field(default_factory=list)
    credits_used: int = 1
    provider: str = ""
    query_text: str = ""


class SearchPort(Protocol):
    """Narrow interface for all search adapters."""

    async def search(self, query: SearchQuery) -> SearchResult:
        """Execute a search and return normalized results."""
        ...

    def capabilities(self) -> dict[str, Any]:
        """Return adapter capability flags.

        Expected keys:
          - native_domain_filter: bool
          - native_date_filter: bool
          - returns_page_content: bool
          - credit_cost_basic: int
          - credit_cost_advanced: int
          - monthly_free_credits: int
        """
        ...
