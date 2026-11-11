"""Pre-flight canary probe — validates all configured providers before a run.

Run from the project root:
    python -m scholarship_finder.preflight

Or as a library:
    from scholarship_finder.preflight import run_preflight
    report = await run_preflight()
    print(report.summary())
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ProviderCheck:
    name: str
    ok: bool
    latency_ms: float = 0.0
    detail: str = ""
    warning: str = ""


@dataclass
class PreflightReport:
    checks: list[ProviderCheck] = field(default_factory=list)
    elapsed_s: float = 0.0

    def all_ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def critical_ok(self) -> bool:
        """At least one LLM model plus at least one working search provider."""
        llm = [c for c in self.checks if ":" in c.name or c.name == "LLM"]
        search = [c for c in self.checks if c.name.startswith("Search (")]
        llm_ok = bool(llm) and any(c.ok for c in llm)
        return llm_ok and any(c.ok for c in search) if search else llm_ok

    def summary(self) -> str:
        lines = [f"Pre-flight check ({self.elapsed_s:.1f}s):"]
        for c in self.checks:
            icon = "[PASS]" if c.ok else "[FAIL]"
            warn = f" [{c.warning}]" if c.warning else ""
            lines.append(f"  {icon} {c.name}: {c.detail}{warn} ({c.latency_ms:.0f}ms)")
        lines.append("PASS" if self.critical_ok() else "FAIL - missing critical providers")
        return "\n".join(lines)


async def run_preflight(settings=None) -> PreflightReport:
    """Run all provider checks asynchronously. Returns a PreflightReport."""
    from scholarship_finder.config import get_settings
    cfg = settings or get_settings()
    report = PreflightReport()
    start = time.perf_counter()

    checks = await asyncio.gather(
        _check_all_models(cfg),
        _check_search_tavily(cfg),
        _check_search_jina(cfg),
        _check_jina_reader(cfg),
        _check_fetch(cfg),
        _check_github(cfg),
        return_exceptions=True,
    )

    for result in checks:
        if isinstance(result, Exception):
            report.checks.append(ProviderCheck(name="unknown", ok=False, detail=str(result)))
        elif isinstance(result, list):
            report.checks.extend(result)
        elif isinstance(result, ProviderCheck):
            report.checks.append(result)
        else:
            logger.warning("Unexpected check result type: %s", type(result))

    report.elapsed_s = time.perf_counter() - start
    return report


# ── Individual provider checks ────────────────────────────────────────────────

async def _check_all_models(cfg) -> list[ProviderCheck]:
    """One tiny generate() per configured model in each chain."""
    out: list[ProviderCheck] = []
    providers: dict[str, object] = {}
    groq_key = getattr(cfg, "groq_api_key", None)
    if groq_key:
        from scholarship_finder.adapters.llm_openai import OpenAICompatProvider
        providers["groq"] = OpenAICompatProvider(
            name="groq", api_key=groq_key,
            base_url=getattr(cfg, "groq_api_base_url", "") or "https://api.groq.com/openai/v1")
    openai_key = getattr(cfg, "openai_api_key", None)
    if openai_key:
        from scholarship_finder.adapters.llm_openai import OpenAICompatProvider
        providers["openai"] = OpenAICompatProvider(
            name="openai", api_key=openai_key,
            base_url=getattr(cfg, "openai_api_base_url", "") or "")
    gemini_key = getattr(cfg, "gemini_api_key", None)
    if gemini_key:
        from scholarship_finder.adapters.llm_gemini import GeminiProvider
        providers["gemini"] = GeminiProvider(api_key=gemini_key)
    if not providers:
        return [ProviderCheck("LLM", ok=False,
                              detail="No LLM keys configured", warning="will use stub")]

    chains = {
        "fast": cfg.fast_models(), "reasoning": cfg.reasoning_models(),
        "vision": cfg.vision_models(),
    }
    seen: set[str] = set()
    for chain_name, models in chains.items():
        for model in models:
            if model in seen:
                continue
            seen.add(model)
            if "gemini" in model:
                pname = "gemini"
            elif "groq" in providers:
                pname = "groq"
            elif "openai" in providers:
                pname = "openai"
            else:
                pname = "gemini" if "gemini" in providers else next(iter(providers))
            provider = providers.get(pname)
            if provider is None:
                out.append(ProviderCheck(f"{chain_name}:{model}", ok=False,
                                       detail=f"no {pname} provider", warning="chain gap"))
                continue
            model_id = model
            t0 = time.perf_counter()
            try:
                from scholarship_finder.ports.llm import LLMTier, TaskSpec
                await asyncio.wait_for(
                    provider.generate(model_id, TaskSpec(
                        tier=LLMTier.FAST, system="Reply with the word PONG only.",
                        max_output_tokens=16)),
                    timeout=20.0)
                ms = (time.perf_counter() - t0) * 1000
                out.append(ProviderCheck(f"{chain_name}:{model}", ok=True,
                                         detail=f"{pname}", latency_ms=ms))
            except Exception as exc:
                ms = (time.perf_counter() - t0) * 1000
                out.append(ProviderCheck(f"{chain_name}:{model}", ok=False,
                                         detail=str(exc)[:90], latency_ms=ms))
    return out


async def _check_search_tavily(cfg) -> ProviderCheck:
    key = getattr(cfg, "tavily_api_key", None)
    if not key:
        return ProviderCheck("Search (Tavily)", ok=False,
                             detail="No TAVILY_API_KEY set", warning="will use stub")
    t0 = time.perf_counter()
    try:
        from scholarship_finder.adapters.search_tavily import TavilySearchAdapter
        from scholarship_finder.ports.search import SearchQuery
        adapter = TavilySearchAdapter(api_key=key)
        result = await asyncio.wait_for(
            adapter.search(SearchQuery(text="test scholarship site:example.com", max_results=1)),
            timeout=10.0,
        )
        ms = (time.perf_counter() - t0) * 1000
        return ProviderCheck("Search (Tavily)", ok=True,
                             detail=f"{len(result.items)} results",
                             latency_ms=ms)
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        return ProviderCheck("Search (Tavily)", ok=False,
                             detail=str(exc)[:80], latency_ms=ms)


async def _check_search_jina(cfg) -> ProviderCheck:
    key = getattr(cfg, "jina_api_key", None)
    if not key:
        return ProviderCheck("Search (Jina)", ok=False,
                             detail="No JINA_API_KEY set", warning="optional")
    t0 = time.perf_counter()
    try:
        from scholarship_finder.adapters.search_jina import JinaSearchAdapter
        from scholarship_finder.ports.search import SearchQuery
        result = await asyncio.wait_for(
            JinaSearchAdapter(api_key=key).search(
                SearchQuery(text="DAAD scholarship international students", max_results=3)),
            timeout=30.0,
        )
        ms = (time.perf_counter() - t0) * 1000
        return ProviderCheck("Search (Jina)", ok=bool(result.items),
                             detail=f"{len(result.items)} results", latency_ms=ms)
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        return ProviderCheck("Search (Jina)", ok=False,
                             detail=str(exc)[:80], latency_ms=ms)


async def _check_jina_reader(cfg) -> ProviderCheck:
    key = (getattr(cfg, "effective_jina_reader_key", lambda: "")()
           or getattr(cfg, "jina_api_key", None))
    if not key:
        return ProviderCheck("Jina reader", ok=False,
                             detail="No JINA key set", warning="tier-1 fetch fallback only")
    t0 = time.perf_counter()
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://r.jina.ai/http%3A%2F%2Fexample.com",
                headers={"Authorization": f"Bearer {key}"})
        ms = (time.perf_counter() - t0) * 1000
        ok = resp.status_code == 200 and len(resp.text) > 50
        return ProviderCheck("Jina reader", ok=ok,
                             detail=f"HTTP {resp.status_code}", latency_ms=ms)
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        return ProviderCheck("Jina reader", ok=False,
                             detail=str(exc)[:80], latency_ms=ms,
                             warning="tier-1 fallback only")


async def _check_fetch(cfg) -> ProviderCheck:
    t0 = time.perf_counter()
    try:
        from scholarship_finder.adapters.fetch_chain import FetchChain
        adapter = FetchChain(settings=cfg)
        doc = await asyncio.wait_for(
            adapter.fetch("https://www.example.com"),
            timeout=12.0,
        )
        ms = (time.perf_counter() - t0) * 1000
        ok = doc.status_class.startswith("2") or doc.status_class.startswith("3")
        return ProviderCheck("Fetch chain", ok=ok,
                             detail=f"tier={doc.tier_used} status={doc.status_class}",
                             latency_ms=ms)
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        return ProviderCheck("Fetch chain", ok=False,
                             detail=str(exc)[:80], latency_ms=ms)


async def _check_github(cfg) -> ProviderCheck:
    token = getattr(cfg, "github_token", None)
    if not token:
        return ProviderCheck("GitHub", ok=False,
                             detail="No GITHUB_TOKEN set", warning="optional")
    t0 = time.perf_counter()
    try:
        from scholarship_finder.adapters.profile_github import GitHubProfileAdapter
        from scholarship_finder.ports.profile_source import ProfileSourceQuery
        adapter = GitHubProfileAdapter(token=token)
        signals = await asyncio.wait_for(
            adapter.fetch_signals(ProfileSourceQuery(handle="octocat")),
            timeout=10.0,
        )
        ms = (time.perf_counter() - t0) * 1000
        return ProviderCheck("GitHub", ok=True,
                             detail=f"repos={len(signals.projects)}",
                             latency_ms=ms)
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        return ProviderCheck("GitHub", ok=False,
                             detail=str(exc)[:80], latency_ms=ms,
                             warning="optional")


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def main() -> None:
    """Console entry point (``scholarship-preflight`` / ``python -m …preflight``)."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    report = asyncio.run(run_preflight())
    try:
        print(report.summary())
    except UnicodeEncodeError:
        plain_summary = report.summary().replace("✅", "[OK]").replace("❌", "[FAIL]")
        print(plain_summary)
    sys.exit(0 if report.critical_ok() else 1)


if __name__ == "__main__":
    main()
