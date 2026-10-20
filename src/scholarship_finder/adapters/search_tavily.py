"""Tavily search adapter — implements the Search port.

Primary search provider. Basic depth = 1 credit; advanced = 2 credits.
Advanced depth used only on retry rounds (architecture §4.3).
All errors mapped to normalized taxonomy.
"""
from __future__ import annotations

import time
from typing import Any

from scholarship_finder.errors import (
    AuthError,
    QuotaExhaustedError,
    RateLimitedError,
    RateLimitScope,
    TransientError,
    UnknownAdapterError,
)
from scholarship_finder.ports.search import SearchPort, SearchQuery, SearchResult, SearchResultItem

try:
    from tavily import TavilyClient, InvalidAPIKeyError, MissingAPIKeyError, UsageLimitExceededError
    _TAVILY_AVAILABLE = True
except ImportError:
    _TAVILY_AVAILABLE = False

PROVIDER = "tavily"


class TavilyAdapter:
    """Tavily search adapter.

    Args:
        api_key: Tavily API key (from TAVILY_API_KEY env var).
    """

    def __init__(self, api_key: str) -> None:
        if not _TAVILY_AVAILABLE:
            raise ImportError("tavily-python not installed. Run: pip install tavily-python")
        self._client = TavilyClient(api_key=api_key)

    async def search(self, query: SearchQuery) -> SearchResult:
        import asyncio

        kwargs: dict[str, Any] = {
            "query": query.text,
            "search_depth": query.depth,       # "basic" | "advanced"
            "max_results": query.max_results,
        }
        # Domain filters
        if query.include_domains:
            kwargs["include_domains"] = query.include_domains
        if query.exclude_domains:
            kwargs["exclude_domains"] = query.exclude_domains
        if query.time_range:
            kwargs["days"] = self._time_range_to_days(query.time_range)

        try:
            raw = await asyncio.to_thread(self._client.search, **kwargs)
        except Exception as exc:
            raise self._map_error(exc) from exc

        items = [
            SearchResultItem(
                url=r.get("url", ""),
                title=r.get("title", ""),
                snippet=r.get("content", ""),
                published_date=r.get("published_date"),
                page_content=r.get("raw_content"),
            )
            for r in raw.get("results", [])
        ]
        credits = 2 if query.depth == "advanced" else 1
        return SearchResult(
            items=items,
            credits_used=credits,
            provider=PROVIDER,
            query_text=query.text,
        )

    def capabilities(self) -> dict[str, Any]:
        return {
            "native_domain_filter": True,
            "native_date_filter": True,
            "returns_page_content": True,
            "credit_cost_basic": 1,
            "credit_cost_advanced": 2,
            "monthly_free_credits": 1000,
            "provider": PROVIDER,
        }

    @staticmethod
    def _time_range_to_days(tr: str) -> int:
        return {"d": 1, "w": 7, "m": 30, "y": 365}.get(tr, 30)

    @staticmethod
    def _map_error(exc: Exception) -> Exception:
        if not _TAVILY_AVAILABLE:
            return UnknownAdapterError(PROVIDER, str(exc), exc)
        msg = str(exc)
        if isinstance(exc, (InvalidAPIKeyError, MissingAPIKeyError)):
            return AuthError(PROVIDER, f"Invalid Tavily API key: {msg}")
        if isinstance(exc, UsageLimitExceededError):
            return QuotaExhaustedError(PROVIDER, f"Tavily monthly credits exhausted: {msg}")
        if "429" in msg:
            return RateLimitedError(PROVIDER, msg, RateLimitScope.MINUTE, 5.0)
        if "5" in msg[:3]:
            return TransientError(PROVIDER, f"Tavily server error: {msg}")
        return UnknownAdapterError(PROVIDER, msg, exc)


# Alias expected by composition root and preflight probe
TavilySearchAdapter = TavilyAdapter

