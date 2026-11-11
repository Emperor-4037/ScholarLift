"""SQLite opportunity knowledge base — profile-independent facts only.

WAL mode, one connection per thread, short transactions. Never stores
profile/assessment/student data.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities(
    canonical_url TEXT PRIMARY KEY, norm_name TEXT, provider TEXT,
    json TEXT, content_hash TEXT, fetched_at REAL, trust TEXT);
CREATE TABLE IF NOT EXISTS page_verdicts(
    url TEXT PRIMARY KEY, content_hash TEXT, verdict TEXT,
    lead_names_json TEXT, reason TEXT, fetched_at REAL);
CREATE TABLE IF NOT EXISTS search_cache(
    key TEXT PRIMARY KEY, results_json TEXT, fetched_at REAL);
CREATE TABLE IF NOT EXISTS page_cache(
    url TEXT PRIMARY KEY, text TEXT, content_hash TEXT, fetched_at REAL);
"""


class KnowledgeBase:
    def __init__(self, path, opp_ttl_days: float = 7.0,
                 search_ttl_hours: float = 24.0) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._opp_ttl = opp_ttl_days * 86400.0
        self._search_ttl = search_ttl_hours * 3600.0
        self._local = threading.local()
        self._conn().executescript(_SCHEMA)
        self._conn().commit()

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self._path), timeout=10)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    # ── opportunities ──────────────────────────────────────────────────────
    def get_opportunity(self, url: str):
        from scholarship_finder.domain.models import Opportunity
        r = self._conn().execute(
            "SELECT json, fetched_at, trust FROM opportunities WHERE canonical_url=?",
            (url,)).fetchone()
        if not r or time.time() - r[1] > self._opp_ttl:
            return None
        try:
            opp = Opportunity.model_validate(json.loads(r[0]))
            import uuid
            return opp.model_copy(update={"id": str(uuid.uuid4())})
        except Exception:
            return None

    def put_opportunity(self, url: str, opp, content_hash: str = "",
                        trust: str = "") -> None:
        import re as _re
        d = opp.model_dump(mode="json")
        norm = _re.sub(r"[^a-z0-9]", "", (opp.name or "").lower())[:80]
        self._conn().execute(
            "INSERT OR REPLACE INTO opportunities VALUES(?,?,?,?,?,?,?)",
            (url, norm,
             opp.institution_or_funder or "", json.dumps(d),
             content_hash, time.time(), trust))
        self._conn().commit()

    # ── page verdicts ──────────────────────────────────────────────────────
    def get_verdict(self, url: str) -> dict | None:
        r = self._conn().execute(
            "SELECT verdict, lead_names_json, reason, fetched_at FROM page_verdicts WHERE url=?",
            (url,)).fetchone()
        if not r or time.time() - r[3] > self._opp_ttl:
            return None
        return {"verdict": r[0],
                "lead_names": json.loads(r[1] or "[]"),
                "reason": r[2] or ""}

    def put_verdict(self, url: str, content_hash: str, verdict: str,
                    lead_names=(), reason: str = "") -> None:
        self._conn().execute(
            "INSERT OR REPLACE INTO page_verdicts VALUES(?,?,?,?,?,?)",
            (url, content_hash, verdict, json.dumps(list(lead_names)),
             reason, time.time()))
        self._conn().commit()

    # ── search cache ───────────────────────────────────────────────────────
    def get_search(self, key: str) -> list[dict] | None:
        r = self._conn().execute(
            "SELECT results_json, fetched_at FROM search_cache WHERE key=?",
            (key,)).fetchone()
        if not r or time.time() - r[1] > self._search_ttl:
            return None
        try:
            return json.loads(r[0])
        except Exception:
            return None

    def put_search(self, key: str, results: list[dict]) -> None:
        self._conn().execute(
            "INSERT OR REPLACE INTO search_cache VALUES(?,?,?)",
            (key, json.dumps(results), time.time()))
        self._conn().commit()

    # ── page cache ─────────────────────────────────────────────────────────
    def get_page(self, url: str) -> str | None:
        r = self._conn().execute(
            "SELECT text, fetched_at FROM page_cache WHERE url=?",
            (url,)).fetchone()
        if not r or time.time() - r[1] > self._opp_ttl:
            return None
        return r[0]

    def put_page(self, url: str, text: str) -> None:
        import hashlib
        self._conn().execute(
            "INSERT OR REPLACE INTO page_cache VALUES(?,?,?,?)",
            (url, text, hashlib.sha256(text.encode()).hexdigest()[:16],
             time.time()))
        self._conn().commit()

    # ── admin ──────────────────────────────────────────────────────────────
    def purge(self) -> None:
        for t in ("opportunities", "page_verdicts", "search_cache", "page_cache"):
            self._conn().execute(f"DELETE FROM {t}")
        self._conn().commit()

    def stats(self) -> dict[str, int]:
        return {t: self._conn().execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("opportunities", "page_verdicts",
                          "search_cache", "page_cache")}


_KB: KnowledgeBase | None = None


def get_kb():
    """Process-wide accessor; None when kb_enabled is false."""
    global _KB
    if _KB is not None:
        return _KB
    try:
        from scholarship_finder.config import get_settings
        cfg = get_settings()
        if not getattr(cfg, "kb_enabled", False):
            return None
        _KB = KnowledgeBase(cfg.kb_path,
                            getattr(cfg, "kb_opp_ttl_days", 7),
                            getattr(cfg, "kb_search_ttl_hours", 24))
        return _KB
    except Exception as exc:
        logger.warning("KB init failed: %s", exc)
        return None


def reset_kb() -> None:
    global _KB
    _KB = None
