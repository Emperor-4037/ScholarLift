"""Profile source port contract — public signal aggregation gateway.

Currently implemented for GitHub REST API.
Extension point for GitLab, Kaggle, personal portfolios, etc.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from scholarship_finder.domain.models import ProfileSignals


@dataclass
class ProfileSourceQuery:
    handle: str                        # GitHub username, GitLab handle, etc.
    source_type: str = "github"        # "github" | "gitlab" | "portfolio"
    max_repos: int = 15
    include_topics: bool = True
    include_languages: bool = True


class ProfileSourcePort(Protocol):
    """Narrow interface for public profile signal sources."""

    async def fetch_signals(self, query: ProfileSourceQuery) -> ProfileSignals:
        """Return public profile signals for the given handle."""
        ...

    def capabilities(self) -> dict[str, Any]:
        """Return capability flags.

        Expected keys:
          - source_type: str
          - auth_required: bool
          - rate_limit_per_hour: int
        """
        ...
