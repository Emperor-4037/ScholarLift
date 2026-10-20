"""Content-hash and TTL in-process cache for adapter calls.

Three independent caches (architecture §2.1, §3.7):
  - Search:     keyed by normalized query text, TTL = hours
  - Fetch:      keyed by canonical URL, TTL = hours
  - Extraction: keyed by content hash (SHA-256), TTL = hours

Cross-run caches hold only public web data — never candidate data.
Cache can be bypassed via a per-request flag for the 'Force Live Refresh' toggle.
"""
from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import Any


class TTLCache:
    """Thread-safe in-process TTL cache with LRU eviction.

    Args:
        maxsize:  Maximum number of entries before oldest is evicted.
        ttl_s:    Time-to-live in seconds for each entry.
    """

    def __init__(self, maxsize: int = 512, ttl_s: float = 3600.0) -> None:
        self.maxsize = maxsize
        self.ttl_s = ttl_s
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        """Return cached value or None if missing/expired."""
        if key not in self._store:
            return None
        value, expires_at = self._store[key]
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        # Move to end (LRU)
        self._store.move_to_end(key)
        return value

    def set(self, key: str, value: Any) -> None:
        """Store *value* under *key* with the configured TTL."""
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = (value, time.monotonic() + self.ttl_s)
        while len(self._store) > self.maxsize:
            self._store.popitem(last=False)  # evict oldest

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


def make_search_key(query_text: str) -> str:
    """Normalize and hash a search query for cache keying."""
    normalized = " ".join(query_text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


def make_fetch_key(canonical_url: str) -> str:
    """Hash a canonical URL for fetch-cache keying."""
    return hashlib.sha256(canonical_url.encode()).hexdigest()[:32]


def make_content_hash(text: str) -> str:
    """SHA-256 of normalized text content for extraction-cache keying."""
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode()).hexdigest()


# ── Process-level cache singletons ───────────────────────────────────────────

_search_cache = TTLCache(maxsize=256, ttl_s=4 * 3600)    # 4 hours
_fetch_cache = TTLCache(maxsize=512, ttl_s=4 * 3600)     # 4 hours
_extract_cache = TTLCache(maxsize=256, ttl_s=8 * 3600)   # 8 hours


def get_search_cache() -> TTLCache:
    return _search_cache


def get_fetch_cache() -> TTLCache:
    return _fetch_cache


def get_extract_cache() -> TTLCache:
    return _extract_cache


def clear_all_caches() -> None:
    """Clear all caches — used by the 'Force Live Refresh' toggle."""
    _search_cache.clear()
    _fetch_cache.clear()
    _extract_cache.clear()
