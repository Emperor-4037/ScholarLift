"""Tavily search adapter — implements the Search port (primary provider).

- ``AsyncTavilyClient`` cached per running event loop.
- ``include_raw_content="markdown"`` on every query: the full page text
  comes back with the result, saving a separate fetch round-trip.
- Returned page content is stored in the **shared fetch cache** keyed by
  canonical URL — the fetch chain then serves it as a tier-0 hit. It is
  never attached to domain objects (Candidates stay small and API-safe).
- Per-run credits are debited through the ambient ``RunContext``
  (``max_search_credits_per_run``): basic = 1, advanced = 2.
- All errors mapped to the normalized taxonomy.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

from scholarship_finder.domain.url import canonicalize_url
from scholarship_finder.engine import context as run_context
from scholarship_finder.errors import (
    AuthError,
    QuotaExhaustedError,
    RateLimitedError,
    RateLimitScope,
    TransientError,
    UnknownAdapterError,
)
from scholarship_finder.ports.fetch import FetchedDocument
from scholarship_finder.ports.search import SearchQuery, SearchResult, SearchResultItem
from scholarship_finder.resilience.cache import get_fetch_cache, make_fetch_key

try:
    from tavily import (
        AsyncTavilyClient,
        InvalidAPIKeyError,
        MissingAPIKeyError,
        UsageLimitExceededError,
    )
    _TAVILY_AVAILABLE = True
except ImportError:  # pragma: no cover
    _TAVILY_AVAILABLE = False

logger = logging.getLogger(__name__)

PROVIDER = "tavily"

# Upper bound on content we keep per page (chars).
_MAX_PAGE_CONTENT = 60_000

_CREDITS = {"basic": 1, "advanced": 2}
_TIME_RANGE = {"d": "day", "w": "week", "m": "month", "y": "year"}
_DEAD_TTL_S = 1800.0


def _kb():
    from scholarship_finder.adapters.knowledge_base import get_kb
    return get_kb()


def search_cache_key(q: SearchQuery) -> str:
    """Stable KB key: everything that changes Tavily's result set."""
    parts = [" ".join(q.text.lower().split()), q.depth, str(q.max_results),
             (q.country or "").lower(), q.time_range or "",
             ",".join(sorted(d.lower() for d in q.include_domains)),
             ",".join(sorted(d.lower() for d in q.exclude_domains))]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class TavilySearchAdapter:
    """Tavily search adapter.

    Args:
        api_key: Tavily API key (TAVILY_API_KEY).
    """

    _dead: tuple[Exception, float] | None = None

    def __init__(self, api_key: str) -> None:
        if not _TAVILY_AVAILABLE:
            raise ImportError("tavily-python not installed. Run: pip install tavily-python")
        self._api_key = api_key
        # Clients are bound to the loop that created them — keep one per loop.
        self._clients: dict[asyncio.AbstractEventLoop, AsyncTavilyClient] = {}
        # Provider-side failure (bad key / plan limit) → fail fast for a while.
        self._dead: tuple[Exception, float] | None = None

    # ── Sticky provider failure ──────────────────────────────────────────────

    def _dead_error(self) -> Exception | None:
        if self._dead is None:
            return None
        err, until = self._dead
        if time.monotonic() >= until:
            self._dead = None
            return None
        return err

    def _kill(self, err: Exception, ttl_s: float = _DEAD_TTL_S) -> None:
        if isinstance(err, (AuthError, QuotaExhaustedError)):
            if self._dead is None:
                logger.warning("Tavily disabled for %.0f min: %s", ttl_s / 60, str(err)[:120])
            self._dead = (err, time.monotonic() + ttl_s)

    # ── Client lifecycle ─────────────────────────────────────────────────────

    def _client(self) -> AsyncTavilyClient:
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = AsyncTavilyClient(api_key=self._api_key)
            self._clients[loop] = client
        return client

    async def aclose(self) -> None:
        """Drop the client bound to the current loop."""
        loop = asyncio.get_running_loop()
        self._clients.pop(loop, None)

    # ── SearchPort ───────────────────────────────────────────────────────────

    async def search(self, query: SearchQuery) -> SearchResult:
        kb = _kb()
        key = search_cache_key(query)
        cached = kb.get_search(key) if kb is not None else None
        if cached is not None:
            items = []
            for d in cached:
                it = SearchResultItem(**d)
                it.page_content = kb.get_page(canonicalize_url(it.url)) or None
                if it.page_content:
                    self._stash_page(it.url, it.title, it.page_content, persist=False)
                items.append(it)
            return SearchResult(items=items, credits_used=0, provider=PROVIDER,
                                query_text=query.text)

        dead = self._dead_error()
        if dead is not None:
            raise dead
        credits = _CREDITS.get(query.depth, 1)
        ctx = run_context.current()
        if ctx is not None and not ctx.try_debit_search(credits):
            raise QuotaExhaustedError(
                PROVIDER,
                f"Per-run search credit budget exhausted ({credits} needed)",
            )

        kwargs: dict[str, Any] = {
            "query": query.text,
            "search_depth": query.depth if query.depth in _CREDITS else "basic",
            "max_results": query.max_results,
            "include_raw_content": "markdown",
            "time_range": _TIME_RANGE.get(query.time_range or "y", "year"),
        }
        if query.include_domains:
            kwargs["include_domains"] = query.include_domains
        if query.exclude_domains:
            kwargs["exclude_domains"] = query.exclude_domains
        if query.country:
            kwargs["country"] = query.country

        try:
            raw = await self._client().search(**kwargs)
        except Exception as first:
            err = self._map_error(first)
            if isinstance(err, (AuthError, QuotaExhaustedError)):
                self._kill(err)
                raise err from first
            # Country codes, time_range or include_raw_content can be rejected
            # by some accounts/SDK combos — retry once with minimal params.
            logger.debug("Tavily search error (%s: %s) — retrying minimal",
                         type(first).__name__, str(first)[:120])
            minimal = {k: kwargs[k] for k in
                       ("query", "search_depth", "max_results")
                       if k in kwargs}
            minimal["include_raw_content"] = "markdown"
            try:
                raw = await self._client().search(**minimal)
            except Exception as exc:
                err = self._map_error(exc)
                self._kill(err)
                raise err from exc

        items: list[SearchResultItem] = []
        for r in raw.get("results", []):
            page_content = (r.get("raw_content") or "")[:_MAX_PAGE_CONTENT] or None
            item = SearchResultItem(
                url=r.get("url", ""),
                title=r.get("title", ""),
                snippet=r.get("content", ""),
                published_date=r.get("published_date"),
                page_content=page_content,
            )
            items.append(item)
            if page_content:
                self._stash_page(item.url, item.title, page_content)

        if kb is not None:
            try:
                kb.put_search(key, [{"url": i.url, "title": i.title, "snippet": i.snippet,
                                     "published_date": i.published_date} for i in items])
            except Exception:
                logger.debug("KB search write failed", exc_info=True)
        return SearchResult(
            items=items,
            credits_used=credits,
            provider=PROVIDER,
            query_text=query.text,
        )

    async def extract_focused(self, urls: list[str], query: str) -> dict[str, str]:
        """Tavily Extract with a relevance query — returns focused chunks per URL.

        Chunks are cached in the KB under ``focused:<url>``; they are never put
        in the fetch cache (that holds the full page, which keeps its links).
        """
        kb = _kb()
        out: dict[str, str] = {}
        misses = []
        for u in urls[:20]:
            hit = kb.get_page("focused:" + u) if kb is not None else None
            if hit:
                out[u] = hit
            else:
                misses.append(u)
        if not misses or self._dead_error() is not None:
            return out
        ctx = run_context.current()
        if ctx is not None and not ctx.try_debit_search(1):
            return out
        try:
            raw = await self._client().extract(
                urls=misses, query=query,
                chunks_per_source=5, extract_depth="basic")
            for r in raw.get("results", []):
                url = r.get("url", "")
                chunks = r.get("chunks") or r.get("raw_content") or ""
                text = "\n\n".join(chunks) if isinstance(chunks, list) else str(chunks)
                if url and text:
                    text = self._fix_mojibake(text)[:6000]
                    out[url] = text
                    if kb is not None:
                        kb.put_page("focused:" + url, text)
        except Exception as exc:
            self._kill(self._map_error(exc))
            logger.debug("extract_focused failed: %r", exc)
        return out

    # ── Fetch tier support (Tavily Extract) ──────────────────────────────────

    async def extract(self, urls: list[str]) -> dict[str, str]:
        """Tavily Extract for URLs that defeated direct fetch. ``{url: text}``."""
        dead = self._dead_error()
        if dead is not None:
            raise dead
        ctx = run_context.current()
        if ctx is not None and not ctx.try_debit_search(1):
            raise QuotaExhaustedError(PROVIDER, "Per-run search credit budget exhausted")
        try:
            raw = await self._client().extract(urls=urls, format="markdown")
        except Exception as exc:
            err = self._map_error(exc)
            self._kill(err)
            raise err from exc
        out: dict[str, str] = {}
        for r in raw.get("results", []):
            url = r.get("url", "")
            text = r.get("raw_content") or ""
            if url and text:
                out[url] = text[:_MAX_PAGE_CONTENT]
        return out

    # ── Internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _fix_mojibake(t: str) -> str:
        if not t:
            return t
        import re as _re
        t = _re.sub(r"(?<=[A-Za-z])�(?=[a-z]|\s|s)", "’", t)
        return t.replace("�", " ")

    @staticmethod
    def _stash_page(url: str, title: str, page_content: str, persist: bool = True) -> None:
        """Store returned page text in the shared fetch cache (tier-0 hit) and,
        when *persist*, in the KB page cache."""
        try:
            canonical = canonicalize_url(url)
            text = TavilySearchAdapter._fix_mojibake(page_content)
            doc = FetchedDocument(
                url=canonical,
                final_url=url,
                status_class="2xx",
                normalized_text=text,
                title=title,
                tier_used=0,
            )
            get_fetch_cache().set(make_fetch_key(canonical), doc)
            kb = _kb() if persist else None
            if kb is not None and text:
                kb.put_page(canonical, text)
        except Exception:
            logger.debug("fetch-cache stash failed for %s", url, exc_info=True)

    @staticmethod
    def _map_error(exc: Exception) -> Exception:
        if not _TAVILY_AVAILABLE:
            return UnknownAdapterError(PROVIDER, str(exc), exc)
        msg = str(exc)
        if isinstance(exc, (InvalidAPIKeyError, MissingAPIKeyError)):
            return AuthError(PROVIDER, f"Invalid Tavily API key: {msg}")
        if isinstance(exc, UsageLimitExceededError):
            return QuotaExhaustedError(PROVIDER, f"Tavily monthly credits exhausted: {msg}")
        low = msg.lower()
        if "432" in msg or "433" in msg or "usage limit" in low:
            return QuotaExhaustedError(PROVIDER, f"Tavily usage limit: {msg}")
        if "429" in msg:
            return RateLimitedError(PROVIDER, msg, RateLimitScope.MINUTE, 5.0)
        if "401" in msg or "403" in msg:
            return AuthError(PROVIDER, msg)
        if "5" in msg[:3]:
            return TransientError(PROVIDER, f"Tavily server error: {msg}")
        return UnknownAdapterError(PROVIDER, msg, exc)
