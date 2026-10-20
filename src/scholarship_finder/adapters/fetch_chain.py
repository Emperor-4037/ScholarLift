"""Tiered fetch chain — implements the Fetch port.

Chain of responsibility (architecture §4.4):
  Tier 0 — TTL cache (free, instant)
  Tier 1 — Jina Reader (keyed, hosted; handles PDFs, JS-rendered pages)
  Tier 2 — Direct HTTP + trafilatura main-content extraction
  Tier 3 — Headless Playwright browser (FULL profile only)

Escalation triggers: 403/429, captcha marker, JS-shell page, empty content.
Per-domain circuit breaker trips on Blocked errors to fast-fail remaining URLs.
SSRF protection: private/loopback addresses rejected before any network call.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from typing import Any

import httpx

from scholarship_finder.domain.url import is_private_or_loopback, extract_domain
from scholarship_finder.errors import (
    BlockedError,
    NotFoundError,
    TimeoutError as SFTimeoutError,
    TransientError,
    UnknownAdapterError,
)
from scholarship_finder.ports.fetch import FetchedDocument, FetchPolicy, FetchPort
from scholarship_finder.resilience.cache import get_fetch_cache, make_fetch_key

try:
    import trafilatura
    _TRAFILATURA = True
except ImportError:
    _TRAFILATURA = False

PROVIDER = "fetch"

_JS_SHELL_MARKERS = [
    "enable javascript", "javascript is required", "javascript is disabled",
    "please enable js", "you need to enable javascript",
]
_CAPTCHA_MARKERS = ["captcha", "robot", "are you human", "cloudflare", "ddos-guard"]
_MIN_CONTENT_CHARS = 200   # below this → likely a JS shell or redirect page


def _is_js_shell(text: str) -> bool:
    t = text.lower()
    return len(text) < _MIN_CONTENT_CHARS or any(m in t for m in _JS_SHELL_MARKERS)


def _is_captcha(text: str) -> bool:
    t = text.lower()
    return any(m in t for m in _CAPTCHA_MARKERS)


def _content_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


# ── Tier 1: Jina Reader ────────────────────────────────────────────────────────

class JinaReaderFetcher:
    """Fetch via Jina Reader API — returns clean Markdown."""

    BASE = "https://r.jina.ai/"

    def __init__(self, api_key: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {api_key}" if api_key else "",
            "X-Return-Format": "markdown",
            "X-No-Cache": "true",
        }
        if not api_key:
            self._headers.pop("Authorization")

    async def fetch(self, url: str, timeout_s: float = 12.0) -> str:
        """Return normalized markdown text or raise a classified error."""
        jina_url = self.BASE + url
        try:
            async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
                resp = await client.get(jina_url, headers=self._headers)
        except httpx.TimeoutException as exc:
            raise SFTimeoutError(PROVIDER, f"Jina Reader timeout for {url}") from exc
        except Exception as exc:
            raise TransientError(PROVIDER, f"Jina Reader connection error: {exc}") from exc

        if resp.status_code == 401:
            raise TransientError(PROVIDER, "Jina: bad API key; falling to Tier 2")
        if resp.status_code == 429:
            raise TransientError(PROVIDER, "Jina: rate limited; falling to Tier 2")
        if resp.status_code in (403, 451):
            raise BlockedError(PROVIDER, f"Jina: access denied for {url}")
        if resp.status_code == 404:
            raise NotFoundError(PROVIDER, f"404 for {url}")
        if resp.status_code >= 500:
            raise TransientError(PROVIDER, f"Jina: server error {resp.status_code}")

        return resp.text


# ── Tier 2: Direct HTTP + trafilatura ────────────────────────────────────────

_DIRECT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ScholarshipFinderBot/1.0; "
        "+https://github.com/scholarship-finder)"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}

_ROBOTS_CACHE: dict[str, bool] = {}  # domain → allowed


async def _check_robots(domain: str, client: httpx.AsyncClient) -> bool:
    """Quick check of robots.txt. Returns True if fetch is allowed."""
    if domain in _ROBOTS_CACHE:
        return _ROBOTS_CACHE[domain]
    try:
        r = await client.get(f"https://{domain}/robots.txt", timeout=3.0)
        allowed = "Disallow: /" not in r.text
        _ROBOTS_CACHE[domain] = allowed
        return allowed
    except Exception:
        return True   # assume allowed on error


class DirectHttpFetcher:
    """Direct HTTP fetch with trafilatura main-content extraction."""

    async def fetch(self, url: str, timeout_s: float = 10.0) -> str:
        domain = extract_domain(url)
        try:
            async with httpx.AsyncClient(
                timeout=timeout_s, follow_redirects=True, max_redirects=5,
                headers=_DIRECT_HEADERS,
            ) as client:
                if not await _check_robots(domain, client):
                    raise BlockedError(PROVIDER, f"robots.txt disallows crawling {domain}")
                resp = await client.get(url)
        except BlockedError:
            raise
        except httpx.TimeoutException as exc:
            raise SFTimeoutError(PROVIDER, f"Direct HTTP timeout for {url}") from exc
        except Exception as exc:
            raise TransientError(PROVIDER, f"Direct HTTP error: {exc}") from exc

        if resp.status_code in (403, 401, 451):
            raise BlockedError(PROVIDER, f"Access denied ({resp.status_code}) for {url}")
        if resp.status_code == 404:
            raise NotFoundError(PROVIDER, f"404 for {url}")
        if resp.status_code == 429:
            raise BlockedError(PROVIDER, f"Rate-limited by server ({url})")
        if resp.status_code >= 500:
            raise TransientError(PROVIDER, f"Server error {resp.status_code} for {url}")

        html = resp.text
        if _TRAFILATURA:
            extracted = trafilatura.extract(
                html,
                include_links=False,
                include_tables=True,
                favor_precision=True,
            )
            return extracted or html[:50_000]
        # Fallback: crude tag strip
        return re.sub(r"<[^>]+>", " ", html)[:50_000]


# ── Main fetch chain ──────────────────────────────────────────────────────────

class FetchChain:
    """Tiered fetch chain implementing the Fetch port.

    Args:
        jina_key:     Jina Reader API key (empty → 20 RPM anonymous).
        enable_browser: Enable Tier-3 Playwright (FULL profile only).
        max_tier:     Override max tier (2 = LITE, 3 = FULL).
    """

    def __init__(
        self,
        jina_key: str = "",
        enable_browser: bool = False,
        max_tier: int = 2,
        settings: Any = None,
    ) -> None:
        if settings is not None:
            jina_key = getattr(settings, "jina_reader_key", "") or jina_key
            enable_browser = getattr(settings, "browser_fetch_enabled", False)
            max_tier = 3 if getattr(settings, "is_full", False) else 2
        self._jina = JinaReaderFetcher(jina_key)
        self._direct = DirectHttpFetcher()
        self._enable_browser = enable_browser and max_tier >= 3
        self._max_tier = max_tier
        self._cache = get_fetch_cache()

    async def fetch(self, url: str, policy: FetchPolicy | None = None) -> FetchedDocument:
        policy = policy or FetchPolicy()

        # SSRF guard
        if is_private_or_loopback(url):
            raise BlockedError(PROVIDER, f"SSRF blocked: {url} resolves to private address")

        cache_key = make_fetch_key(url)

        # Tier 0: Cache
        if policy.allow_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        deadline = time.monotonic() + policy.timeout_s
        text = ""
        tier_used = 1
        final_url = url
        truncated = False

        # Tier 1: Jina Reader
        try:
            text = await asyncio.wait_for(
                self._jina.fetch(url, timeout_s=min(12.0, policy.timeout_s)),
                timeout=min(13.0, policy.timeout_s),
            )
            tier_used = 1
        except (SFTimeoutError, TransientError, NotFoundError):
            pass
        except BlockedError:
            raise   # propagate to domain breaker immediately
        except asyncio.TimeoutError:
            pass

        # Tier 2: Direct HTTP (if Jina failed or returned JS shell)
        if not text or _is_js_shell(text) or _is_captcha(text):
            if time.monotonic() < deadline - 2.0:
                try:
                    remaining = deadline - time.monotonic()
                    text = await asyncio.wait_for(
                        self._direct.fetch(url, timeout_s=min(10.0, remaining)),
                        timeout=min(11.0, remaining),
                    )
                    tier_used = 2
                except (SFTimeoutError, NotFoundError, BlockedError):
                    raise
                except Exception:
                    text = ""

        # Tier 3: Playwright browser (FULL only)
        if (not text or _is_js_shell(text)) and self._enable_browser:
            if time.monotonic() < deadline - 3.0:
                text = await self._browser_fetch(url)
                tier_used = 3

        if not text:
            raise NotFoundError(PROVIDER, f"All fetch tiers failed for {url}")

        # Truncate to policy.max_bytes (rough char approximation)
        if len(text) > policy.max_bytes:
            text = text[: policy.max_bytes]
            truncated = True

        doc = FetchedDocument(
            url=url,
            final_url=final_url,
            status_class="2xx",
            normalized_text=text,
            tier_used=tier_used,
            truncated=truncated,
            content_hash=_content_hash(text),
        )

        # Cache successful result
        if policy.allow_cache:
            self._cache.set(cache_key, doc)

        return doc

    async def _browser_fetch(self, url: str) -> str:
        """Tier-3: Playwright headless browser (FULL profile only)."""
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle", timeout=20_000)
                content = await page.content()
                await browser.close()
                if _TRAFILATURA:
                    return trafilatura.extract(content) or content[:50_000]
                return re.sub(r"<[^>]+>", " ", content)[:50_000]
        except Exception as exc:
            raise TransientError(PROVIDER, f"Browser fetch failed: {exc}") from exc

    def capabilities(self) -> dict[str, Any]:
        return {
            "max_tier": self._max_tier,
            "handles_pdf": True,
            "handles_js": self._enable_browser,
        }
