"""GitHub REST API v3 adapter — implements the Profile Source port.

Fetches public repos, languages, topics, and star counts for a GitHub handle.
Uses a server-side token (public-read scope) to avoid shared-IP 60 req/hour limit.
All failures are non-blocking: the run continues without GitHub signals.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from scholarship_finder.domain.models import ProfileSignals
from scholarship_finder.errors import (
    AuthError,
    NotFoundError,
    RateLimitedError,
    RateLimitScope,
    TimeoutError as SFTimeoutError,
    TransientError,
)
from scholarship_finder.ports.profile_source import ProfileSourceQuery

PROVIDER = "github"
_API_BASE = "https://api.github.com"
_TIMEOUT = 6.0   # per-call timeout (architecture §1.6)


class GitHubAdapter:
    """GitHub REST API v3 adapter for public profile enrichment.

    Args:
        token: GitHub personal access token (public-read scope).
               Empty string → unauthenticated (60 req/hr shared IP cap).
        cache: Optional TTLCache for caching GitHub profiles (Phase 1 optimization).
    """

    def __init__(self, token: str = "", cache=None) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._headers = headers
        self._cache = cache

    async def fetch_signals(self, query: ProfileSourceQuery) -> ProfileSignals:
        handle = query.handle.strip().lstrip("@")

        # Check cache first (Phase 1 optimization)
        cache_key = f"github:{handle}"
        if self._cache is not None:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        async with httpx.AsyncClient(
            base_url=_API_BASE,
            headers=self._headers,
            timeout=_TIMEOUT,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),  # Connection pooling
        ) as client:
            repos, languages, topics = await asyncio.gather(
                self._fetch_repos(client, handle, query.max_repos),
                asyncio.sleep(0),   # placeholder for parallel lang aggregation
                asyncio.sleep(0),
            )

        # Aggregate languages and topics across repos
        lang_counts: dict[str, int] = {}
        topic_counts: dict[str, int] = {}
        project_summaries: list[dict[str, Any]] = []

        for repo in repos:
            name = repo.get("name", "")
            desc = repo.get("description", "") or ""
            stars = repo.get("stargazers_count", 0)
            lang = repo.get("language") or ""
            repo_topics = repo.get("topics", [])

            if lang:
                lang_counts[lang] = lang_counts.get(lang, 0) + 1
            for t in repo_topics:
                topic_counts[t] = topic_counts.get(t, 0) + 1

            project_summaries.append({
                "name": name,
                "description": desc,
                "stars": stars,
                "language": lang,
                "topics": repo_topics,
                "url": repo.get("html_url", ""),
            })

        sorted_langs = [l for l, _ in sorted(lang_counts.items(), key=lambda x: -x[1])]
        sorted_topics = [t for t, _ in sorted(topic_counts.items(), key=lambda x: -x[1])][:20]

        activity = (
            f"{len(repos)} public repos; top languages: {', '.join(sorted_langs[:5]) or 'none'}; "
            f"top topics: {', '.join(sorted_topics[:5]) or 'none'}"
        )

        signals = ProfileSignals(
            projects=project_summaries,
            languages=sorted_langs,
            topics=sorted_topics,
            activity_summary=activity,
        )

        # Cache the result (Phase 1 optimization)
        if self._cache is not None:
            self._cache.set(cache_key, signals)

        return signals

    def capabilities(self) -> dict[str, Any]:
        return {
            "source_type": "github",
            "auth_required": False,
            "rate_limit_per_hour": 5000,   # authenticated; 60 unauthenticated
        }

    async def _fetch_repos(
        self, client: httpx.AsyncClient, handle: str, max_repos: int
    ) -> list[dict[str, Any]]:
        try:
            resp = await client.get(
                f"/users/{handle}/repos",
                params={
                    "type": "owner",
                    "sort": "updated",
                    "per_page": min(max_repos, 100),
                },
            )
        except httpx.TimeoutException as exc:
            raise SFTimeoutError(PROVIDER, f"GitHub timeout for {handle}") from exc
        except Exception as exc:
            raise TransientError(PROVIDER, f"GitHub connection error: {exc}") from exc

        if resp.status_code == 404:
            raise NotFoundError(PROVIDER, f"GitHub handle '{handle}' not found")
        if resp.status_code == 401:
            raise AuthError(PROVIDER, "GitHub token invalid or expired")
        if resp.status_code == 403:
            ra = float(resp.headers.get("Retry-After", "60"))
            raise RateLimitedError(
                PROVIDER, "GitHub rate limit hit", RateLimitScope.MINUTE, ra
            )
        if resp.status_code >= 500:
            raise TransientError(PROVIDER, f"GitHub server error {resp.status_code}")

        return resp.json()


# Alias expected by composition root and preflight probe
GitHubProfileAdapter = GitHubAdapter

