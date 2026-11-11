import pytest

from scholarship_finder.adapters import search_tavily as st
from scholarship_finder.engine import context as run_context
from scholarship_finder.ports.search import SearchQuery

pytestmark = pytest.mark.skipif(not st._TAVILY_AVAILABLE, reason="tavily not installed")

LONG = "Scholarship for master's students. " * 30


class FakeClient:
    def __init__(self):
        self.searches = self.extracts = 0

    async def search(self, **kw):
        self.searches += 1
        return {"results": [
            {"url": "https://a.edu/x", "title": "X", "content": "c", "raw_content": LONG},
            {"url": "https://b.edu/y", "title": "Y", "content": "c", "raw_content": LONG}]}

    async def extract(self, **kw):
        self.extracts += 1
        return {"results": [{"url": u, "chunks": ["eligibility chunk " * 5]} for u in kw["urls"]]}


def _adapter():
    a = st.TavilySearchAdapter(api_key="x")
    a.fake = FakeClient()
    a._client = lambda: a.fake
    return a


async def test_identical_search_is_cached_and_free():
    a = _adapter()
    ctx = run_context.RunContext(run_id="t")
    tok = run_context.set_context(ctx)
    try:
        r1 = await a.search(SearchQuery(text="chevening masters"))
        used = ctx.search_credits
        r2 = await a.search(SearchQuery(text="  Chevening   masters "))
        assert a.fake.searches == 1 and used >= 1 and ctx.search_credits == used
        assert r2.credits_used == 0
        assert [i.url for i in r2.items] == [i.url for i in r1.items]
        assert all(i.page_content for i in r2.items)
        await a.search(SearchQuery(text="chevening masters", include_domains=["a.edu"]))
        await a.search(SearchQuery(text="chevening masters", country="germany"))
        assert a.fake.searches == 3
    finally:
        run_context.reset_context(tok)


async def test_extract_focused_served_from_kb():
    a = _adapter()
    urls = ["https://a.edu/x", "https://b.edu/y"]
    first = await a.extract_focused(urls, "eligibility")
    second = await a.extract_focused(urls, "eligibility")
    assert a.fake.extracts == 1 and first == second and set(second) == set(urls)
