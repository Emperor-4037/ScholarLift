"""Tiered fetch chain — implements the Fetch port.

Tiers:
  0 — TTL cache (keyed by canonical URL; also fed by Tavily raw content)
  1 — Jina Reader (keyed, hosted, 12 s)
  2 — Direct HTTP + robots.txt + trafilatura / PyMuPDF for PDFs
  3 — Tavily Extract (≤5 URLs per run)
  4 — Headless Playwright (FULL deployment profile only)

Safety: SSRF is checked twice — literal hostname/IP check *and* a real
``getaddrinfo`` resolution (in a thread) before any socket opens.
Per-domain circuit breakers (threshold 2) fast-fail hostile domains.
All httpx clients are cached per running event loop.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
import time
import urllib.robotparser
from typing import Any
from urllib.parse import urlparse

import httpx

from scholarship_finder.domain.url import (
    canonicalize_url,
    extract_domain,
    is_private_or_loopback,
)
from scholarship_finder.engine import context as run_context
from scholarship_finder.errors import (
    BlockedError,
    NotFoundError,
    TimeoutError as SFTimeoutError,
    TransientError,
)
from scholarship_finder.ports.fetch import FetchedDocument, FetchPolicy
from scholarship_finder.resilience.breaker import get_breaker_registry
from scholarship_finder.resilience.cache import get_fetch_cache, make_fetch_key

try:
    import trafilatura
    _TRAFILATURA = True
except ImportError:  # pragma: no cover
    _TRAFILATURA = False

try:
    import fitz  # PyMuPDF
    _PYMUPDF = True
except ImportError:  # pragma: no cover
    _PYMUPDF = False

logger = logging.getLogger(__name__)

PROVIDER = "fetch"

_JINA_BASE = "https://r.jina.ai/"
_JINA_TIMEOUT_S = 20.0
_MAX_TAVILY_EXTRACT_PER_RUN = 5
_ROBOTS_TTL_S = 3600.0

_JS_SHELL_MARKERS = [
    "enable javascript", "javascript is required", "javascript is disabled",
    "please enable js", "you need to enable javascript",
]
_CAPTCHA_MARKERS = ["captcha", "are you human", "cloudflare", "ddos-guard"]
_MIN_CONTENT_CHARS = 200   # below this → likely a JS shell or redirect page

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _fix_mojibake(t: str) -> str:
    """Sanitize U+FFFD at the fetch boundary: letter-adjacent → apostrophe."""
    if not t:
        return t
    t = re.sub(r"(?<=[A-Za-z])�(?=[a-z]|\s|s)", "’", t)
    return t.replace("�", " ")


def _is_js_shell(text: str) -> bool:
    t = text.lower()
    return len(text) < _MIN_CONTENT_CHARS or any(m in t for m in _JS_SHELL_MARKERS)


def _is_captcha(text: str) -> bool:
    t = text.lower()
    return any(m in t for m in _CAPTCHA_MARKERS)


def _content_hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def _is_private_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True   # unparsable → treat as unsafe
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


class FetchChain:
    """Tiered fetch chain implementing the Fetch port.

    Args:
        settings:      Settings (jina key fallback, deployment profile).
        tavily:        Optional TavilySearchAdapter for the Extract tier.
        jina_key:      Explicit Jina Reader key (overrides settings).
        enable_browser: Force-enable the Playwright tier.
    """

    def __init__(
        self,
        settings: Any = None,
        tavily: Any = None,
        jina_key: str = "",
        enable_browser: bool = False,
    ) -> None:
        key = jina_key
        if settings is not None:
            key = key or getattr(settings, "effective_jina_reader_key", lambda: "")()
            if callable(key) is False and not isinstance(key, str):
                key = ""
        self._jina_key = key or ""
        self._tavily = tavily
        self._enable_browser = enable_browser or bool(
            settings is not None and getattr(settings, "is_full_profile", lambda: False)()
        )
        self._cache = get_fetch_cache()
        self._breakers = get_breaker_registry()
        # Per-loop resources
        self._clients: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}
        # domain → (RobotFileParser | None, fetched_at); None = permissive on error
        self._robots: dict[str, tuple[urllib.robotparser.RobotFileParser | None, float]] = {}
        # run_id → number of Tavily Extract calls used
        self._extract_used: dict[str, int] = {}

    # ── Client lifecycle ─────────────────────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = httpx.AsyncClient(
                timeout=10.0,
                follow_redirects=True,
                max_redirects=5,
                headers={
                    "User-Agent": _UA,
                    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                limits=httpx.Limits(max_connections=15, max_keepalive_connections=5),
            )
            self._clients[loop] = client
        return client

    async def aclose(self) -> None:
        """Close httpx clients bound to the current loop."""
        loop = asyncio.get_running_loop()
        client = self._clients.pop(loop, None)
        if client is not None:
            try:
                await client.aclose()
            except Exception:
                logger.debug("FetchChain: client close failed", exc_info=True)
        # Old alias kept for callers that used close()
    close = aclose

    # ── FetchPort ────────────────────────────────────────────────────────────

    async def fetch(self, url: str, policy: FetchPolicy | None = None) -> FetchedDocument:
        policy = policy or FetchPolicy()
        canonical = canonicalize_url(url)
        domain = extract_domain(canonical)

        # ── SSRF guard 1: literal hostname/IP ────────────────────────────────
        if is_private_or_loopback(canonical):
            raise BlockedError(PROVIDER, f"SSRF blocked: {canonical}")

        # ── Per-domain breaker ───────────────────────────────────────────────
        breaker = self._breakers.get_domain(domain)
        if breaker.is_open():
            raise BlockedError(PROVIDER, f"Domain breaker open for {domain}")

        cache_key = make_fetch_key(canonical)
        if policy.allow_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        # ── SSRF guard 2: real DNS resolution ────────────────────────────────
        if not await self._dns_safe(canonical):
            raise BlockedError(PROVIDER, f"SSRF blocked: {canonical} resolves privately")

        deadline = time.monotonic() + policy.timeout_s
        text = ""
        tier_used = 1
        final_url = canonical

        # ── Tier 1: Jina Reader ──────────────────────────────────────────────
        try:
            text = await asyncio.wait_for(
                self._jina_fetch(canonical),
                timeout=max(min(_JINA_TIMEOUT_S + 1.0, policy.timeout_s), 20.0),
            )
            tier_used = 1
        except BlockedError:
            breaker.record_failure(BlockedError(PROVIDER, "jina blocked"))
            raise
        except Exception:
            text = ""

        # ── Tier 2: Direct HTTP ──────────────────────────────────────────────
        if not text or _is_js_shell(text) or _is_captcha(text):
            if time.monotonic() < deadline - 2.0:
                try:
                    remaining = deadline - time.monotonic()
                    doc = await asyncio.wait_for(
                        self._direct_fetch(canonical),
                        timeout=min(10.0, remaining),
                    )
                    text, final_url = doc
                    tier_used = 2
                except BlockedError as exc:
                    breaker.record_failure(exc)
                    raise
                except (NotFoundError, SFTimeoutError):
                    raise
                except Exception:
                    text = ""

        # ── Tier 3: Tavily Extract (≤5 per run) ─────────────────────────────
        if not text and self._tavily is not None:
            run_id = run_context.current().run_id if run_context.current() else "_"
            used = self._extract_used.get(run_id, 0)
            if used < _MAX_TAVILY_EXTRACT_PER_RUN and time.monotonic() < deadline - 2.0:
                self._extract_used[run_id] = used + 1
                try:
                    results = await self._tavily.extract([final_url])
                    text = results.get(final_url) or next(iter(results.values()), "")
                    if text:
                        tier_used = 3
                except Exception:
                    text = ""

        # ── Tier 4: Playwright (FULL profile only) ───────────────────────────
        if (not text or _is_js_shell(text)) and self._enable_browser:
            if time.monotonic() < deadline - 3.0:
                try:
                    text = await self._browser_fetch(canonical)
                    if text:
                        tier_used = 4
                except Exception:
                    text = ""

        if not text:
            raise NotFoundError(PROVIDER, f"All fetch tiers failed for {canonical}")

        truncated = False
        if len(text) > policy.max_bytes:
            text = text[: policy.max_bytes]
            truncated = True

        doc = FetchedDocument(
            url=canonical,
            final_url=final_url,
            status_class="2xx",
            normalized_text=_fix_mojibake(text),
            tier_used=tier_used,
            truncated=truncated,
            content_hash=_content_hash(text),
        )
        breaker.record_success()
        if policy.allow_cache:
            self._cache.set(cache_key, doc)
        return doc

    def capabilities(self) -> dict[str, Any]:
        return {
            "max_tier": 4 if self._enable_browser else 3,
            "handles_pdf": True,
            "handles_js": self._enable_browser,
        }

    # ── Tiers ────────────────────────────────────────────────────────────────

    async def _jina_fetch(self, url: str) -> str:
        headers = {"X-Return-Format": "markdown"}
        if self._jina_key and self._jina_key.isascii():
            headers["Authorization"] = f"Bearer {self._jina_key}"
        try:
            resp = await self._client().get(_JINA_BASE + url, headers=headers,
                                            timeout=_JINA_TIMEOUT_S)
        except httpx.TimeoutException as exc:
            raise SFTimeoutError(PROVIDER, f"Jina Reader timeout for {url}") from exc
        except Exception as exc:
            raise TransientError(PROVIDER, f"Jina Reader error: {exc}") from exc

        if resp.status_code in (401, 429):
            raise TransientError(PROVIDER, f"Jina: {resp.status_code}; falling to tier 2")
        if resp.status_code in (403, 451):
            raise BlockedError(PROVIDER, f"Jina: access denied for {url}")
        if resp.status_code == 404:
            raise NotFoundError(PROVIDER, f"404 for {url}")
        if resp.status_code >= 500:
            raise TransientError(PROVIDER, f"Jina: server error {resp.status_code}")
        return resp.text

    async def _direct_fetch(self, url: str) -> tuple[str, str]:
        """Direct HTTP GET → (text, final_url). Honours robots.txt."""
        domain = extract_domain(url)
        if not await self._robots_allowed(domain, url):
            raise BlockedError(PROVIDER, f"robots.txt disallows {url}")

        try:
            resp = await self._client().get(url, timeout=9.0)
        except httpx.TimeoutException as exc:
            raise SFTimeoutError(PROVIDER, f"Direct HTTP timeout for {url}") from exc
        except Exception as exc:
            raise TransientError(PROVIDER, f"Direct HTTP error: {exc}") from exc

        if resp.status_code in (401, 403, 451):
            raise BlockedError(PROVIDER, f"Access denied ({resp.status_code}) for {url}")
        if resp.status_code == 404:
            raise NotFoundError(PROVIDER, f"404 for {url}")
        if resp.status_code == 429:
            raise BlockedError(PROVIDER, f"Rate-limited by server ({url})")
        if resp.status_code >= 500:
            raise TransientError(PROVIDER, f"Server error {resp.status_code} for {url}")

        ctype = (resp.headers.get("content-type") or "").lower()
        body = resp.content
        if "application/pdf" in ctype or body[:5] == b"%PDF-":
            return await asyncio.to_thread(self._pdf_to_text, body), str(resp.url)

        html = resp.text
        if _TRAFILATURA:
            extracted = await asyncio.to_thread(
                trafilatura.extract, html,
                include_links=False, include_tables=True, favor_precision=True,
            )
            return (extracted or html[:50_000]), str(resp.url)
        return re.sub(r"<[^>]+>", " ", html)[:50_000], str(resp.url)

    @staticmethod
    def _pdf_to_text(pdf_bytes: bytes) -> str:
        if not _PYMUPDF:
            return ""
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            parts = [page.get_text() for page in doc[:15]]
            doc.close()
            return "\n\n".join(p for p in parts if p.strip())
        except Exception:
            return ""

    async def _browser_fetch(self, url: str) -> str:
        """Tier-4: Playwright headless browser (FULL profile only)."""
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(user_agent=_UA)
                await page.goto(url, wait_until="networkidle", timeout=20_000)
                content = await page.content()
                await browser.close()
        except Exception as exc:
            raise TransientError(PROVIDER, f"Browser fetch failed: {exc}") from exc
        if _TRAFILATURA:
            extracted = await asyncio.to_thread(trafilatura.extract, content)
            return extracted or content[:50_000]
        return re.sub(r"<[^>]+>", " ", content)[:50_000]

    # ── robots.txt ───────────────────────────────────────────────────────────

    async def _robots_allowed(self, domain: str, url: str) -> bool:
        """Real ``urllib.robotparser`` check, cached per domain for 1 h.

        Permissive on any fetch/parse error — a missing or broken robots.txt
        must not block fetches. A ``Disallow: /private`` entry blocks only
        that subtree, never the whole site.
        """
        cached = self._robots.get(domain)
        if cached is None or time.monotonic() - cached[1] >= _ROBOTS_TTL_S:
            rp: urllib.robotparser.RobotFileParser | None = None
            try:
                resp = await self._client().get(f"https://{domain}/robots.txt", timeout=4.0)
                if resp.status_code < 400:
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(resp.text.splitlines())
            except Exception:
                rp = None   # permissive on fetch error
            cached = (rp, time.monotonic())
            self._robots[domain] = cached
        parser = cached[0]
        if parser is None:
            return True
        return parser.can_fetch(_UA, url)

    # ── SSRF: DNS resolution check ───────────────────────────────────────────

    async def _dns_safe(self, url: str) -> bool:
        """Resolve the host and reject when every/any answer is private."""
        host = urlparse(url).hostname or ""
        if not host:
            return False
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo, host, None, proto=socket.IPPROTO_TCP
            )
        except (socket.gaierror, OSError):
            return False   # unresolvable → let the tier report NotFound upstream
        if not infos:
            return False
        return all(not _is_private_ip(info[4][0]) for info in infos)
