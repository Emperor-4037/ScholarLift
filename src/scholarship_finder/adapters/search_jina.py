"""Jina Search adapter — secondary search provider (s.jina.ai).

Shares the free Jina API key already used for the Jina Reader fetch tier.
Used for Retry Round 1 to diversify the search index (architecture §4.3, OPEN-3).
Also supports Serper as an alternative via the same adapter interface.
"""
from __future__ import annotations

import asyncio
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

_JINA_TIMEOUT_S = 25.0
_JINA_MAX_SITES = 5
_JINA_RETRY_STATUS = (429, 500, 502, 503, 504)


def jina_sites(domains: list[str]) -> list[str]:
    """Usable ``site`` filters: strip ``*.``, drop bare TLD labels, dedupe, cap."""
    out: list[str] = []
    for d in domains:
        d = (d or "").strip().lower().split("://")[-1].split("/")[0]
        if d.startswith("*."):
            d = d[2:]
        if d.startswith("www."):
            d = d[4:]
        if "." in d and d not in out:
            out.append(d)
    return out[:_JINA_MAX_SITES]


class JinaSearchAdapter:
    """Secondary search via Jina Search API (s.jina.ai).

    Returns Markdown-enriched results. Shares the Jina API key. Several
    ``site`` filters may be sent at once (repeated param); latency is spiky
    (2-20 s) so calls get a generous timeout and one retry.
    """

    def __init__(self, api_key: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = _JINA_TIMEOUT_S,
                 max_concurrency: int = 6) -> None:
        self._api_key = api_key or ""
        self._transport = transport
        self._timeout = timeout
        self._max_concurrency = max_concurrency
        self._sems: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}
        self._headers: dict[str, str] = {
            "Accept": "application/json",
            "X-Respond-With": "no-content",   # snippets only; save tokens
        }
        if self._api_key and self._api_key.isascii():
            self._headers["Authorization"] = f"Bearer {self._api_key}"

    def _sem(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        sem = self._sems.get(loop)
        if sem is None:
            sem = self._sems[loop] = asyncio.Semaphore(self._max_concurrency)
        return sem

    async def search(self, query: SearchQuery) -> SearchResult:
        params: list[tuple[str, str]] = [("q", query.text), ("count", str(query.max_results))]
        params += [("site", s) for s in jina_sites(query.include_domains)]
        async with self._sem():
            data = await self._get(params)

        items = [
            SearchResultItem(
                url=r.get("url", ""),
                title=r.get("title", ""),
                snippet=r.get("description", "") or r.get("content", ""),
            )
            for r in data.get("data", []) if r.get("url")
        ]
        return SearchResult(items=items, credits_used=0, provider=PROVIDER_JINA, query_text=query.text)

    async def _get(self, params: list[tuple[str, str]]) -> dict[str, Any]:
        for attempt in (0, 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout,
                                             transport=self._transport) as client:
                    resp = await client.get(_JINA_SEARCH_URL, params=params,
                                            headers=self._headers)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status == 422 and "no search results" in exc.response.text.lower():
                    return {"data": []}
                if attempt == 0 and status in _JINA_RETRY_STATUS:
                    try:
                        wait = min(float(exc.response.headers.get("Retry-After", "")), 5.0)
                    except ValueError:
                        wait = 1.5
                    await asyncio.sleep(wait)
                    continue
                raise self._map_http_error(exc, PROVIDER_JINA) from exc
            except httpx.TransportError as exc:
                if attempt == 0:
                    continue
                raise TransientError(PROVIDER_JINA, f"{type(exc).__name__}: {exc}") from exc
            except Exception as exc:
                raise UnknownAdapterError(PROVIDER_JINA, str(exc), exc) from exc
        raise TransientError(PROVIDER_JINA, "retries exhausted")

    def capabilities(self) -> dict[str, Any]:
        return {
            "native_domain_filter": True,
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
        if status in (401, 403):
            return AuthError(provider, f"Invalid API key: {msg}")
        if status == 429:
            return RateLimitedError(provider, msg, RateLimitScope.MINUTE, 5.0)
        if status in (500, 502, 503, 504):
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
