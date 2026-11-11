"""Step-3 tests: local provider, ETA scheduler, server manager, time budget."""
import asyncio
import pytest
from unittest import mock

from scholarship_finder.adapters.llm_local import LocalLlamaProvider
from scholarship_finder.adapters.llm_router import LLMRouter
from scholarship_finder.adapters.llama_server import LlamaServerManager
from scholarship_finder.ports.llm import LLMResult, TaskSpec
from scholarship_finder.domain.models import LLMTier
from scholarship_finder.resilience.breaker import BreakerRegistry
from scholarship_finder.resilience.quota import ModelLimiter


def _spec(**kw):
    return TaskSpec(tier=LLMTier.FAST, system="s", prompt="p",
                    json_mode=True, max_output_tokens=100, **kw)


class FakeLLM:
    """Stub provider returning canned completions; records calls."""
    def __init__(self, name="fake", delay=0.0, fail=None):
        self.name, self.delay, self.fail = name, delay, fail
        self.calls = []

    async def generate(self, model, spec):
        self.calls.append(model)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return LLMResult(content='{"a":1}', parsed={"a": 1},
                         provider=self.name, model=model, tier=spec.tier)


class FakeSettings:
    def __init__(self, fast=None, reasoning=None):
        self._fast = fast or []
        self._reasoning = reasoning or []
        self.local_llm_parallel = 3
        self.local_llm_max_input_tokens = 4500

    def fast_models(self):
        return self._fast

    def reasoning_models(self):
        return self._reasoning

    def vision_models(self):
        return []

    def model_limits(self, p, m):
        return {}


def _router(settings, providers):
    return LLMRouter(settings=settings, providers=providers,
                     limiter=ModelLimiter(), breakers=BreakerRegistry())


# ── LocalLlamaProvider ──────────────────────────────────────────────────────

def test_local_provider_sends_schema():
    p = LocalLlamaProvider()
    captured = {}
    async def fake_call(model, kwargs):
        captured.update(kwargs)
        class C: pass
        c = C()
        c.choices = [type("x", (), {"message": type("m", (), {"content": "{}"})(),
                                    "finish_reason": "stop"})()]
        c.usage = None
        return c, None
    p._call = fake_call
    spec = _spec(json_schema={"type": "object"})
    asyncio.get_event_loop().run_until_complete(p.generate("m", spec))
    assert captured["response_format"]["schema"] == {"type": "object"}


def test_local_empty_content_transient():
    from scholarship_finder.errors import TransientError
    p = LocalLlamaProvider()
    async def fake_call(model, kwargs):
        c = type("C", (), {})()
        c.choices = [type("x", (), {"message": type("m", (), {"content": ""})(),
                                  "finish_reason": "length"})()]
        c.usage = None
        return c, None
    p._call = fake_call
    with pytest.raises(TransientError):
        asyncio.get_event_loop().run_until_complete(p.generate("m", _spec()))


# ── Router ETA scheduler ────────────────────────────────────────────────────

def test_local_chosen_when_apis_saturated():
    settings = FakeSettings(fast=["local:q7", "groq:m1"])
    router = _router(settings, {"local": FakeLLM("local"),
                              "groq": FakeLLM("groq")})
    # Saturate the API model's bucket.
    router._limiter.reserve("m1", 10_000)
    r = asyncio.get_event_loop().run_until_complete(router.complete(_spec()))
    assert r.provider == "local"


def test_api_chosen_when_local_down():
    settings = FakeSettings(fast=["local:q7", "groq:m1"])
    router = _router(settings, {"local": FakeLLM("local"),
                              "groq": FakeLLM("groq")})
    router._local_down_until = 10**12   # far future → local skipped
    r = asyncio.get_event_loop().run_until_complete(router.complete(_spec()))
    assert r.provider == "groq"


def test_context_guard_blocks_big_local_call():
    settings = FakeSettings(fast=["local:q7", "groq:m1"])
    settings.local_llm_max_input_tokens = 10
    router = _router(settings, {"local": FakeLLM("local"),
                              "groq": FakeLLM("groq")})
    r = asyncio.get_event_loop().run_until_complete(router.complete(_spec()))
    assert r.provider == "groq"


def test_quality_floor_prefers_api_for_sop():
    settings = FakeSettings(reasoning=["groq:m1", "local:q7"],
                            fast=["groq:m1", "local:q7"])
    router = _router(settings, {"local": FakeLLM("local"),
                              "groq": FakeLLM("groq")})
    spec = TaskSpec(tier=LLMTier.REASONING, system="s", prompt="p",
                    max_output_tokens=100, purpose="sop_generation")
    r = asyncio.get_event_loop().run_until_complete(router.complete(spec))
    assert r.provider == "groq"          # API ETA < 1.5×local+20 → API wins


# ── LlamaServerManager ──────────────────────────────────────────────────────

def test_manager_no_spawn_when_healthy():
    cfg = mock.Mock(local_llm_enabled=True, local_llm_autostart=True,
                    local_llm_base_url="http://127.0.0.1:8088/v1",
                    local_llm_server_path="x", local_llm_model_path="m",
                    local_llm_ctx=8192, local_llm_parallel=3,
                    local_llm_extra_args="-fa on")
    mgr = LlamaServerManager(cfg)
    async def healthy(**k): return True
    mgr.healthy = healthy
    ok = asyncio.get_event_loop().run_until_complete(mgr.start())
    assert ok and mgr._proc is None    # no spawn


def test_manager_command_build():
    cfg = mock.Mock(local_llm_enabled=True, local_llm_autostart=True,
                    local_llm_base_url="http://127.0.0.1:8088/v1",
                    local_llm_server_path="C:/llama/llama-server.exe",
                    local_llm_model_path="C:/m.gguf",
                    local_llm_ctx=18432, local_llm_parallel=3,
                    local_llm_extra_args="-fa on")
    cmd = LlamaServerManager(cfg).command()
    assert cmd[0].endswith("llama-server.exe") and "-np" in cmd and "3" in cmd
    assert "--port" in cmd and "8088" in cmd


# ── Time budget: audit stops at discovery deadline, partial kept ────────────

async def test_auditor_stops_at_discovery_deadline():
    from scholarship_finder.agents.source_auditor import SourceAuditorAgent
    from scholarship_finder.domain.models import Candidate, SwarmState, TrustTier
    from scholarship_finder.engine import context as rc

    class SlowLLM:
        async def complete_json(self, spec):
            await asyncio.sleep(60)          # far beyond deadline
            r = LLMResult(content="", provider="f", model="m")
            r.parsed = {"pages": []}
            return r

    class F:
        async def fetch(self, u, policy=None):
            from scholarship_finder.ports.fetch import FetchedDocument
            return FetchedDocument(url=u, final_url=u, status_class="2xx",
                                   normalized_text="t " * 1000)

    cands = [Candidate(url=f"https://x{i}.edu/s",
                       canonical_url=f"https://x{i}.edu/s",
                       domain=f"x{i}.edu",
                       trust_tier=TrustTier.OFFICIAL_INSTITUTION)
             for i in range(6)]
    st = SwarmState(candidates=cands)
    ctx = rc.RunContext(run_id="t", hard_deadline_s=360, reserve_s=358)
    ctx.started -= 10            # elapsed 10 s → discovery_time_left ≈ −8
    token = rc._current.set(ctx)
    try:
        ag = SourceAuditorAgent(SlowLLM(), F())
        out = await ag.run(st, None)
    finally:
        rc._current.reset(token)
    # Discovery budget exhausted → auditor returns quickly with no crash.
    assert "opportunities" in out
