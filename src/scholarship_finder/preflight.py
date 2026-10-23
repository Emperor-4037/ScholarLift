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
import sys
import time
from dataclasses import dataclass, field


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
        """Core providers (LLM + Search) must pass for the system to work."""
        critical = [c for c in self.checks if c.name in ("LLM (Gemini)", "Search (Tavily)")]
        return all(c.ok for c in critical)

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
        _check_llm_gemini(cfg),
        _check_llm_groq(cfg),
        _check_search_tavily(cfg),
        _check_search_jina(cfg),
        _check_fetch(cfg),
        _check_github(cfg),
        return_exceptions=True,
    )

    for result in checks:
        if isinstance(result, Exception):
            report.checks.append(ProviderCheck(name="unknown", ok=False, detail=str(result)))
        elif isinstance(result, ProviderCheck):
            report.checks.append(result)
        else:
            logger.warning("Unexpected check result type: %s", type(result))

    report.elapsed_s = time.perf_counter() - start
    return report


# ── Individual provider checks ────────────────────────────────────────────────

async def _check_llm_gemini(cfg) -> ProviderCheck:
    key = getattr(cfg, "gemini_api_key", None)
    if not key:
        return ProviderCheck("LLM (Gemini)", ok=False, detail="No GEMINI_API_KEY set",
                             warning="will use stub")
    t0 = time.perf_counter()
    try:
        from scholarship_finder.adapters.llm_gemini import GeminiLLMAdapter
        from scholarship_finder.ports.llm import TaskSpec, LLMTier
        adapter = GeminiLLMAdapter(api_key=key, settings=cfg)
        result = await asyncio.wait_for(
            adapter.complete(TaskSpec(
                tier=LLMTier.FAST,
                instruction="Reply with the word PONG only.",
                data_blocks=[],
                max_output_tokens=50,
            )),
            timeout=18.0,
        )
        ms = (time.perf_counter() - t0) * 1000
        return ProviderCheck("LLM (Gemini)", ok=True,
                             detail=f"model={result.model}",
                             latency_ms=ms)
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        return ProviderCheck("LLM (Gemini)", ok=False,
                             detail=str(exc)[:80], latency_ms=ms)


async def _check_llm_groq(cfg) -> ProviderCheck:
    key = getattr(cfg, "groq_api_key", None) or getattr(cfg, "openai_api_key", None)
    if not key:
        return ProviderCheck("LLM Failover (Groq)", ok=False, detail="No GROQ_API_KEY set",
                             warning="optional failover")
    t0 = time.perf_counter()
    try:
        from scholarship_finder.adapters.llm_openai import OpenAILLMAdapter
        from scholarship_finder.ports.llm import TaskSpec, LLMTier
        adapter = OpenAILLMAdapter(api_key=key, settings=cfg)
        result = await asyncio.wait_for(
            adapter.complete(TaskSpec(
                tier=LLMTier.FAST,
                instruction="Reply with the word PONG only.",
                data_blocks=[],
                max_output_tokens=50,
            )),
            timeout=15.0,
        )
        ms = (time.perf_counter() - t0) * 1000
        return ProviderCheck("LLM Failover (Groq)", ok=True,
                             detail=f"model={result.model}",
                             latency_ms=ms)
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        return ProviderCheck("LLM Failover (Groq)", ok=False,
                             detail=str(exc)[:80], latency_ms=ms,
                             warning="failover offline")


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
                             detail="No JINA_API_KEY set", warning="secondary only")
    t0 = time.perf_counter()
    try:
        from scholarship_finder.adapters.search_jina import JinaSearchAdapter
        from scholarship_finder.ports.search import SearchQuery
        adapter = JinaSearchAdapter(api_key=key)
        result = await asyncio.wait_for(
            adapter.search(SearchQuery(text="scholarship", max_results=1)),
            timeout=10.0,
        )
        ms = (time.perf_counter() - t0) * 1000
        return ProviderCheck("Search (Jina)", ok=True,
                             detail=f"{len(result.items)} results", latency_ms=ms)
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        return ProviderCheck("Search (Jina)", ok=False,
                             detail=str(exc)[:80], latency_ms=ms,
                             warning="secondary only")


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

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    report = asyncio.run(run_preflight())
    try:
        print(report.summary())
    except UnicodeEncodeError:
        # Fallback for terminals that cannot handle unicode emojis
        plain_summary = report.summary().replace("✅", "[OK]").replace("❌", "[FAIL]")
        print(plain_summary)
    sys.exit(0 if report.critical_ok() else 1)
