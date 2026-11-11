"""Tests for TavilySearchAdapter — stubbed client, no network.

Verifies: include_raw_content="markdown" is requested, returned page content
is stashed in the shared fetch cache keyed by canonical URL (NOT attached to
domain objects), credits accounting, and extract().
"""
from __future__ import annotations

import asyncio

import pytest

from scholarship_finder.adapters.search_tavily import TavilySearchAdapter
from scholarship_finder.domain.url import canonicalize_url
from scholarship_finder.errors import QuotaExhaustedError
from scholarship_finder.engine.context import RunContext, set_context, reset_context
from scholarship_finder.ports.search import SearchQuery
from scholarship_finder.resilience.cache import get_fetch_cache, make_fetch_key


class FakeTavilyClient:
    def __init__(self, response=None, extract_response=None):
        self.response = response or {}
        self.extract_response = extract_response or {}
        self.search_calls: list[dict] = []
        self.extract_calls: list[dict] = []

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return self.response

    async def extract(self, **kwargs):
        self.extract_calls.append(kwargs)
        return self.extract_response


def _adapter(client: FakeTavilyClient) -> TavilySearchAdapter:
    adapter = TavilySearchAdapter(api_key="tvly-test")
    loop = asyncio.get_event_loop_policy().get_event_loop()
    adapter._clients[loop] = client
    return adapter


async def test_search_requests_raw_markdown():
    client = FakeTavilyClient({"results": []})
    adapter = _adapter(client)
    await adapter.search(SearchQuery(text="cs scholarships", depth="advanced", max_results=5))
    kwargs = client.search_calls[0]
    assert kwargs["include_raw_content"] == "markdown"
    assert kwargs["search_depth"] == "advanced"
    assert kwargs["max_results"] == 5
    assert kwargs["time_range"] == "year"


async def test_raw_content_stashed_in_fetch_cache():
    url = "https://www.example.com/scholarship?utm_source=x&a=1"
    page = "# Official page\nFull details here" * 20
    client = FakeTavilyClient({
        "results": [{
            "url": url,
            "title": "Example Scholarship",
            "content": "snippet",
            "raw_content": page,
        }]
    })
    adapter = _adapter(client)

    result = await adapter.search(SearchQuery(text="q"))
    assert result.items[0].page_content == page
    assert result.credits_used == 1

    canonical = canonicalize_url(url)
    cached = get_fetch_cache().get(make_fetch_key(canonical))
    assert cached is not None, "raw content must land in the shared fetch cache"
    assert cached.normalized_text == page
    assert cached.tier_used == 0


async def test_search_credit_budget_blocks():
    client = FakeTavilyClient({"results": []})
    adapter = _adapter(client)
    ctx = RunContext(run_id="t", max_search_credits=1)
    token = set_context(ctx)
    try:
        await adapter.search(SearchQuery(text="a", depth="basic"))
        with pytest.raises(QuotaExhaustedError):
            await adapter.search(SearchQuery(text="b", depth="advanced"))
    finally:
        reset_context(token)


class DeadTavilyClient(FakeTavilyClient):
    async def search(self, **kwargs):
        from tavily import InvalidAPIKeyError
        self.search_calls.append(kwargs)
        raise InvalidAPIKeyError("Unauthorized: missing or invalid API key.")

    async def extract(self, **kwargs):
        self.extract_calls.append(kwargs)
        raise AssertionError("must not be called once the provider is dead")


async def test_invalid_key_is_sticky_and_fails_fast():
    from scholarship_finder.errors import AuthError
    client = DeadTavilyClient()
    adapter = _adapter(client)
    with pytest.raises(AuthError):
        await adapter.search(SearchQuery(text="a"))
    assert len(client.search_calls) == 1, "401 must not trigger the minimal-params retry"
    with pytest.raises(AuthError):
        await adapter.search(SearchQuery(text="b"))
    assert len(client.search_calls) == 1, "second call must not hit the network"
    with pytest.raises(AuthError):
        await adapter.extract(["https://x.com/p"])
    assert await adapter.extract_focused(["https://x.com/p"], "q") == {}
    assert client.extract_calls == []


async def test_dead_provider_still_serves_search_cache():
    client = FakeTavilyClient({"results": [{"url": "https://a.edu/s", "title": "S", "content": "c"}]})
    adapter = _adapter(client)
    q = SearchQuery(text="cached query")
    await adapter.search(q)
    adapter._kill(QuotaExhaustedError("tavily", "usage limit"))
    res = await adapter.search(q)
    assert [i.url for i in res.items] == ["https://a.edu/s"] and res.credits_used == 0


async def test_extract_returns_url_text_map():
    client = FakeTavilyClient(
        extract_response={"results": [{"url": "https://x.com/p", "raw_content": "text"}]}
    )
    adapter = _adapter(client)
    out = await adapter.extract(["https://x.com/p"])
    assert out == {"https://x.com/p": "text"}
    assert client.extract_calls[0]["format"] == "markdown"
