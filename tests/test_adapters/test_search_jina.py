"""JinaSearchAdapter — MockTransport, no network."""
from __future__ import annotations

import httpx
import pytest

from scholarship_finder.adapters.search_jina import JinaSearchAdapter, jina_sites
from scholarship_finder.errors import AuthError, TransientError
from scholarship_finder.ports.search import SearchQuery

_OK = {"data": [{"url": "https://www.daad.de/x", "title": "T", "description": "d"},
                {"url": "", "title": "no url"}]}


def test_jina_sites_filters_wildcards_and_bare_labels():
    assert jina_sites(["*.gc.ca", "daad.de", "edu", "*.gov", "DAAD.de", " "]) == ["gc.ca", "daad.de"]
    assert len(jina_sites([f"d{i}.org" for i in range(9)])) == 5


async def test_repeated_site_params_and_result_mapping():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params.multi_items())
        return httpx.Response(200, json=_OK)

    ad = JinaSearchAdapter(api_key="k", transport=httpx.MockTransport(handler))
    res = await ad.search(SearchQuery(text="master", include_domains=["*.gc.ca", "daad.de", "edu"]))
    assert [v for k, v in seen[0] if k == "site"] == ["gc.ca", "daad.de"]
    assert [i.url for i in res.items] == ["https://www.daad.de/x"]
    assert res.credits_used == 0


async def test_one_retry_on_429_then_success():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "0"}) if len(calls) == 1 \
            else httpx.Response(200, json=_OK)

    ad = JinaSearchAdapter(api_key="k", transport=httpx.MockTransport(handler))
    res = await ad.search(SearchQuery(text="q"))
    assert len(calls) == 2 and res.items


async def test_double_timeout_raises_transient():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    ad = JinaSearchAdapter(api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(TransientError):
        await ad.search(SearchQuery(text="q"))


async def test_422_no_results_is_an_empty_result_not_an_error():
    body = {"code": 422, "message": "AssertionFailureError: No search results available for query x"}
    ad = JinaSearchAdapter(api_key="k", transport=httpx.MockTransport(
        lambda req: httpx.Response(422, json=body)))
    res = await ad.search(SearchQuery(text="x", include_domains=["daad.de"]))
    assert res.items == []


def test_jina_sites_normalises_scheme_path_and_www():
    assert jina_sites(["https://www.DAAD.de/en/", "www.hea.ie"]) == ["daad.de", "hea.ie"]


async def test_401_is_auth_error_without_retry():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401)

    ad = JinaSearchAdapter(api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(AuthError):
        await ad.search(SearchQuery(text="q"))
    assert len(calls) == 1
