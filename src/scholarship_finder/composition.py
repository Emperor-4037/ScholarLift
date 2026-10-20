"""Composition Root — wires all adapters, ports, and agents into a RunGateway.

This is the ONLY place where concrete adapter classes are instantiated.
All other modules depend on port interfaces only.

Usage (LITE profile):
    from scholarship_finder.composition import build_gateway
    gateway = build_gateway()
    run_id = await gateway.start_run(initial_state)

The returned RunGateway is safe to store as a module-level singleton in the
Streamlit app (it is async-safe and stateless between runs).
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from scholarship_finder.config import get_settings
from scholarship_finder.engine.gateway import RunGateway

logger = logging.getLogger(__name__)


def build_gateway(settings=None) -> RunGateway:
    """Build a fully-wired RunGateway from configuration.

    Adapters are constructed lazily and fall back gracefully when API keys
    are missing (DEMO/REPLAY mode will still work without live keys).
    """
    cfg = settings or get_settings()

    # ── LLM adapters ──────────────────────────────────────────────────────────
    llm_port = _build_llm_port(cfg)

    # ── Search adapters ───────────────────────────────────────────────────────
    search_port, secondary_search = _build_search_ports(cfg)

    # ── Fetch adapter ─────────────────────────────────────────────────────────
    fetch_port = _build_fetch_port(cfg)

    # ── Document adapter ──────────────────────────────────────────────────────
    document_port = _build_document_port(cfg)

    # ── Profile source adapter ────────────────────────────────────────────────
    profile_source = _build_profile_source(cfg)

    # ── Resilience stack ──────────────────────────────────────────────────────
    from scholarship_finder.resilience.breaker import BreakerRegistry
    from scholarship_finder.resilience.quota import QuotaGovernor, QuotaProfile

    breaker_registry = BreakerRegistry()
    quota_governor = QuotaGovernor(
        profiles=_default_quota_profiles(cfg),
        reserve_ratio=getattr(cfg, "judge_reserve_ratio", 0.30),
        max_reasoning_per_run=getattr(cfg, "max_reasoning_calls_per_run", 4),
        is_judge=False,
    )

    # ── Agents ────────────────────────────────────────────────────────────────
    from scholarship_finder.agents.profile_enricher import ProfileEnricherAgent
    from scholarship_finder.agents.opportunity_scout import OpportunityScoutAgent
    from scholarship_finder.agents.source_auditor import SourceAuditorAgent
    from scholarship_finder.agents.ranking_strategist import RankingStrategistAgent
    from scholarship_finder.agents.application_strategist import ApplicationStrategistAgent

    profile_enricher = ProfileEnricherAgent(llm_port, profile_source)
    opportunity_scout = OpportunityScoutAgent(llm_port, search_port, secondary_search)
    source_auditor = SourceAuditorAgent(llm_port, fetch_port)
    ranking_strategist = RankingStrategistAgent(llm_port)
    application_strategist = ApplicationStrategistAgent(llm_port)

    # ── Intake service ─────────────────────────────────────────────────────────
    from scholarship_finder.agents.intake import IntakeService
    intake_service = IntakeService(
        document_port=document_port,
        vault_ttl_seconds=getattr(cfg, "pii_vault_ttl_seconds", 900),
    )

    # ── Graph runner factory ───────────────────────────────────────────────────
    def make_graph_runner(
        scrubbed_text: str,
        github_username: str | None,
        vault,
        bus,
        is_replay: bool = False,
    ):
        from scholarship_finder.engine.graph import make_graph
        return make_graph(
            profile_enricher_agent=profile_enricher,
            opportunity_scout_agent=opportunity_scout,
            source_auditor_agent=source_auditor,
            ranking_strategist_agent=ranking_strategist,
            application_strategist_agent=application_strategist,
            bus=bus,
            scrubbed_text=scrubbed_text,
            github_username=github_username,
            vault=vault,
            is_replay=is_replay,
        )

    # ── RunGateway ────────────────────────────────────────────────────────────
    gateway = RunGateway(
        graph_runner=None,  # injected per-run via start_run_with_intake
        max_concurrent=getattr(cfg, "max_concurrent_runs", 3),
    )

    # Attach helpers as convenience attributes
    gateway.intake_service = intake_service  # type: ignore[attr-defined]
    gateway.make_graph_runner = make_graph_runner  # type: ignore[attr-defined]

    logger.info(
        "Composition: gateway built (profile=%s, llm=%s, search=%s)",
        getattr(cfg, "deployment_profile", "LITE"),
        type(llm_port).__name__,
        type(search_port).__name__,
    )
    return gateway


async def start_run_with_intake(
    gateway: RunGateway,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    display_name: str | None = None,
    github_username: str | None = None,
    is_replay: bool = False,
) -> str:
    """Convenience coroutine: run intake → wire graph → start run. Returns run_id."""
    from scholarship_finder.engine.gateway import make_initial_state

    # Intake (PII scrub, PDF extract)
    intake_result, vault = await gateway.intake_service.process(
        file_bytes, filename, mime_type, display_name=display_name
    )

    initial_state = make_initial_state()
    run_id = initial_state.run_meta.run_id

    # Build a shared EventBus so the graph runner and gateway polling loop
    # see the SAME events (fixes the two-bus bug).
    from scholarship_finder.engine.event_bus import EventBus
    bus = EventBus()

    # Build graph runner for this run (per-run, no global mutation)
    graph_runner = gateway.make_graph_runner(
        scrubbed_text=intake_result.scrubbed_text,
        github_username=github_username,
        vault=vault,
        bus=bus,
        is_replay=is_replay,
    )

    # Pass bus and runner directly — no global _graph_runner mutation
    return await gateway.start_run(
        initial_state,
        is_replay=is_replay,
        graph_runner=graph_runner,
        bus=bus,
    )


# ── Internal adapter builders ─────────────────────────────────────────────────

def _build_llm_port(cfg):
    """Try Gemini first, fall back to OpenAI-compatible, then stub."""
    gemini_key = getattr(cfg, "gemini_api_key", None)
    if gemini_key:
        try:
            from scholarship_finder.adapters.llm_gemini import GeminiLLMAdapter
            logger.info("LLM: using Gemini adapter")
            return GeminiLLMAdapter(api_key=gemini_key, settings=cfg)
        except Exception as exc:
            logger.warning("Gemini adapter failed to init: %s", exc)

    openai_key = getattr(cfg, "openai_api_key", None)
    if openai_key:
        try:
            from scholarship_finder.adapters.llm_openai import OpenAILLMAdapter
            logger.info("LLM: using OpenAI-compatible adapter")
            return OpenAILLMAdapter(api_key=openai_key, settings=cfg)
        except Exception as exc:
            logger.warning("OpenAI adapter failed to init: %s", exc)

    logger.warning("LLM: no API keys configured — using stub (REPLAY/test only)")
    return _StubLLMPort()


def _build_search_ports(cfg):
    tavily_key = getattr(cfg, "tavily_api_key", None)
    primary = None
    if tavily_key:
        try:
            from scholarship_finder.adapters.search_tavily import TavilySearchAdapter
            primary = TavilySearchAdapter(api_key=tavily_key)
            logger.info("Search: primary=Tavily")
        except Exception as exc:
            logger.warning("Tavily adapter failed: %s", exc)

    secondary = None
    secondary_provider = getattr(cfg, "search_secondary_provider", "jina")
    if secondary_provider == "serper":
        serper_key = getattr(cfg, "serper_api_key", None)
        if serper_key:
            try:
                from scholarship_finder.adapters.search_jina import SerperAdapter
                secondary = SerperAdapter(api_key=serper_key)
                logger.info("Search: secondary=Serper")
            except Exception as exc:
                logger.warning("Serper adapter failed: %s", exc)
    else:
        jina_key = getattr(cfg, "jina_api_key", None)
        try:
            from scholarship_finder.adapters.search_jina import JinaSearchAdapter
            secondary = JinaSearchAdapter(api_key=jina_key)
            logger.info("Search: secondary=Jina")
        except Exception as exc:
            logger.warning("Jina adapter failed: %s", exc)

    if primary is None:
        logger.warning("Search: no primary search configured — using stub")
        primary = _StubSearchPort()

    return primary, secondary


def _build_fetch_port(cfg):
    try:
        from scholarship_finder.adapters.fetch_chain import FetchChain
        logger.info("Fetch: using FetchChain")
        return FetchChain(settings=cfg)
    except Exception as exc:
        logger.warning("FetchChain init failed: %s — using stub", exc)
        return _StubFetchPort()


def _build_document_port(cfg):
    try:
        from scholarship_finder.adapters.document_pdf import PDFDocumentAdapter
        return PDFDocumentAdapter()
    except Exception as exc:
        logger.warning("PDF adapter init failed: %s — using stub", exc)
        return _StubDocumentPort()


def _build_profile_source(cfg):
    github_token = getattr(cfg, "github_token", None)
    if github_token:
        try:
            from scholarship_finder.adapters.profile_github import GitHubProfileAdapter
            logger.info("ProfileSource: using GitHub adapter")
            return GitHubProfileAdapter(token=github_token)
        except Exception as exc:
            logger.warning("GitHub adapter failed: %s", exc)
    return None


def _default_quota_profiles(cfg) -> list:
    from scholarship_finder.resilience.quota import QuotaProfile
    return [
        QuotaProfile(provider="gemini", model="gemini-2.0-flash-lite", rpm=30, rpd=1500),
        QuotaProfile(provider="gemini", model="gemini-2.0-flash", rpm=15, rpd=1500),
        QuotaProfile(provider="gemini", model="gemini-2.5-flash", rpm=10, rpd=500),
        QuotaProfile(provider="openai", model="gpt-4o-mini", rpm=60, rpd=3000),
        QuotaProfile(provider="tavily", model="search", rpm=100, rpd=5000),
    ]


# ── Stub ports (used when no API keys are configured) ─────────────────────────

class _StubLLMPort:
    """No-op LLM — returns empty content. For REPLAY/test mode."""
    async def complete(self, spec):
        from scholarship_finder.ports.llm import LLMResult, LLMUsage, LLMTier
        return LLMResult(content="{}", usage=LLMUsage(), provider="stub", tier=LLMTier.FAST)

    async def stream(self, spec):
        yield ""

    def capabilities(self):
        return {"native_json_schema": False, "streaming": False, "vision": False,
                "max_context_tokens": 0, "max_output_tokens": 0,
                "reasoning_control": False, "may_train": None, "data_policy": "stub"}


class _StubSearchPort:
    async def search(self, query):
        from scholarship_finder.ports.search import SearchResult
        return SearchResult(items=[], provider="stub", query_text=query.text)

    def capabilities(self):
        return {"native_domain_filter": False, "native_date_filter": False,
                "returns_page_content": False, "credit_cost_basic": 0,
                "credit_cost_advanced": 0, "monthly_free_credits": 0}


class _StubFetchPort:
    async def fetch(self, url, policy=None):
        from scholarship_finder.ports.fetch import FetchedDocument
        return FetchedDocument(url=url, final_url=url, status_class="error",
                               normalized_text="", tier_used=0)

    def capabilities(self):
        return {"max_tier": 0, "handles_pdf": False, "handles_js": False}


class _StubDocumentPort:
    async def extract(self, pdf_bytes, max_pages=50):
        from scholarship_finder.ports.document import DocumentResult
        return DocumentResult(pages=[], total_pages=0, backend_used="stub")

    def capabilities(self):
        return {"ocr_capable": False, "max_file_bytes": 0, "backend": "stub"}
