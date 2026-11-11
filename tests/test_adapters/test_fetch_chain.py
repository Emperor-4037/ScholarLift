"""Tests for the FetchChain — robots.txt semantics and SSRF, no network."""
from __future__ import annotations

import time
import urllib.robotparser

import pytest

from scholarship_finder.adapters.fetch_chain import FetchChain
from scholarship_finder.errors import BlockedError


def _chain() -> FetchChain:
    return FetchChain()


def _seed_robots(chain: FetchChain, domain: str, robots_text: str) -> None:
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(robots_text.splitlines())
    chain._robots[domain] = (rp, time.monotonic())


# ── robots.txt semantics ─────────────────────────────────────────────────────

async def test_disallow_subpath_does_not_block_root():
    """`Disallow: /private` must not block `/` — the old code blocked everything."""
    chain = _chain()
    _seed_robots(chain, "example.com", "User-agent: *\nDisallow: /private\n")
    assert await chain._robots_allowed("example.com", "https://example.com/") is True
    assert await chain._robots_allowed("example.com", "https://example.com/public/x") is True
    assert await chain._robots_allowed("example.com", "https://example.com/private/x") is False


async def test_disallow_all_blocks_everything():
    chain = _chain()
    _seed_robots(chain, "blocked.example", "User-agent: *\nDisallow: /\n")
    assert await chain._robots_allowed("blocked.example", "https://blocked.example/") is False
    assert await chain._robots_allowed("blocked.example", "https://blocked.example/any") is False


async def test_empty_robots_allows():
    chain = _chain()
    _seed_robots(chain, "open.example", "")
    assert await chain._robots_allowed("open.example", "https://open.example/x") is True


async def test_missing_parser_is_permissive():
    chain = _chain()
    chain._robots["err.example"] = (None, time.monotonic())
    assert await chain._robots_allowed("err.example", "https://err.example/x") is True


# ── SSRF ─────────────────────────────────────────────────────────────────────

async def test_ssrf_loopback_ip_blocked():
    chain = _chain()
    with pytest.raises(BlockedError, match="SSRF"):
        await chain.fetch("http://127.0.0.1/admin")


async def test_ssrf_private_ip_blocked():
    chain = _chain()
    with pytest.raises(BlockedError, match="SSRF"):
        await chain.fetch("http://192.168.1.1/router")


async def test_ssrf_localhost_blocked():
    chain = _chain()
    with pytest.raises(BlockedError, match="SSRF"):
        await chain.fetch("http://localhost:8080/internal")


async def test_ssrf_linklocal_blocked():
    chain = _chain()
    with pytest.raises(BlockedError, match="SSRF"):
        await chain.fetch("http://169.254.169.254/latest/meta-data")


def test_is_private_ip_helper():
    from scholarship_finder.adapters.fetch_chain import _is_private_ip
    assert _is_private_ip("10.0.0.1") is True
    assert _is_private_ip("172.16.5.5") is True
    assert _is_private_ip("8.8.8.8") is False
    assert _is_private_ip("not-an-ip") is True
