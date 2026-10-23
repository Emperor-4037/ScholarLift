"""Jina Search adapter — secondary search provider (s.jina.ai).

Shares the free Jina API key already used for the Jina Reader fetch tier.
Used for Retry Round 1 to diversify the search index (architecture §4.3, OPEN-3).
Also supports Serper as an alternative via the same adapter interface.
"""
from __future__ import annotations

from typing import Any

import httpx

from scholarship_finder.errors import (
    AuthError,
    RateLimitedError,
    RateLimitScope,
    TransientError,
    UnknownAdapterError,
)
from scholarship_finder.ports.search import SearchQuery, SearchResult, SearchResultItem

PROVIDER_JINA = "jina_search"
PROVIDER_SERPER = "serper"

_JINA_SEARCH_URL = "https://s.jina.ai/"
_SERPER_URL = "https://google.serper.dev/search"


class JinaSearchAdapter:
    """Secondary search via Jina Search API (s.jina.ai).

    Returns Markdown-enriched results. Shares the Jina API key.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or ""
        self._headers: dict[str, str] = {
            "Accept": "application/json",
            "X-Respond-With": "no-content",   # snippets only; save tokens
        }
        if self._api_key:
            self._headers["Authorization"] = f"Bearer {self._api_key}"

    async def search(self, query: SearchQuery) -> SearchResult:
        params: dict[str, Any] = {"q": query.text, "count": query.max_results}
        if query.include_domains:
            params["site"] = query.include_domains[0]   # Jina supports one site filter

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    _JINA_SEARCH_URL,
                    params=params,
                    headers=self._headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise self._map_http_error(exc, PROVIDER_JINA) from exc
        except Exception as exc:
            raise UnknownAdapterError(PROVIDER_JINA, str(exc), exc) from exc

        items = [
            SearchResultItem(
                url=r.get("url", ""),
                title=r.get("title", ""),
                snippet=r.get("description", "") or r.get("content", ""),
            )
            for r in data.get("data", [])
        ]
        return SearchResult(items=items, credits_used=0, provider=PROVIDER_JINA, query_text=query.text)

    def capabilities(self) -> dict[str, Any]:
        return {
            "native_domain_filter": False,
            "native_date_filter": False,
            "returns_page_content": False,
            "credit_cost_basic": 0,
            "credit_cost_advanced": 0,
            "monthly_free_credits": 10_000_000,   # token-based, not credit-based
            "provider": PROVIDER_JINA,
        }

    @staticmethod
    def _map_http_error(exc: httpx.HTTPStatusError, provider: str) -> Exception:
        status = exc.response.status_code
        msg = str(exc)
        if status == 401:
            return AuthError(provider, f"Invalid API key: {msg}")
        if status == 429:
            return RateLimitedError(provider, msg, RateLimitScope.MINUTE, 5.0)
        if status in (500, 502, 503):
            return TransientError(provider, f"Server error {status}: {msg}")
        return UnknownAdapterError(provider, msg, exc)


class SerperAdapter:
    """Secondary search via Serper.dev (Google SERP API).

    2,500 free queries/month; no billing card required.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def search(self, query: SearchQuery) -> SearchResult:
        # Build query with site: operators for domain filters
        q = query.text
        if query.include_domains:
            site_filter = " OR ".join(f"site:{d}" for d in query.include_domains[:3])
            q = f"{q} ({site_filter})"

        payload = {"q": q, "num": query.max_results}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    _SERPER_URL,
                    json=payload,
                    headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise JinaSearchAdapter._map_http_error(exc, PROVIDER_SERPER) from exc
        except Exception as exc:
            raise UnknownAdapterError(PROVIDER_SERPER, str(exc), exc) from exc

        items = [
            SearchResultItem(
                url=r.get("link", ""),
                title=r.get("title", ""),
                snippet=r.get("snippet", ""),
            )
            for r in data.get("organic", [])
        ]
        return SearchResult(items=items, credits_used=1, provider=PROVIDER_SERPER, query_text=query.text)

    def capabilities(self) -> dict[str, Any]:
        return {
            "native_domain_filter": False,
            "native_date_filter": True,
            "returns_page_content": False,
            "credit_cost_basic": 1,
            "credit_cost_advanced": 1,
            "monthly_free_credits": 2500,
            "provider": PROVIDER_SERPER,
        }
