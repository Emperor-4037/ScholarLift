"""Composition Root — wires all adapters, ports, and agents into a RunGateway.

This is the ONLY place where concrete adapter classes are instantiated.
All other modules depend on port interfaces only.

Usage:
    from scholarship_finder.composition import build_gateway
    gateway = build_gateway()
    run_id = await gateway.start_run(initial_state)

The returned RunGateway is safe to store as a module-level singleton
(it is async-safe and stateless between runs).
"""
from __future__ import annotations

import asyncio
import logging

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
    from scholarship_finder.resilience.quota import QuotaGovernor
    from scholarship_finder.resilience.cache import get_github_cache

    breaker_registry = BreakerRegistry()
    quota_governor = QuotaGovernor(
        profiles=_default_quota_profiles(cfg),
        reserve_ratio=getattr(cfg, "judge_reserve_ratio", 0.30),
        max_reasoning_per_run=getattr(cfg, "max_reasoning_calls_per_run", 4),
        is_judge=False,
    )

    # Cross-run caches
    github_cache = get_github_cache()

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
        force_refresh: bool = False,
    ):
        from scholarship_finder.engine.graph import make_graph

        # Inject EventBus into LLM adapter for error reporting
        if hasattr(llm_port, '_bus'):
            llm_port._bus = bus

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
            force_refresh=force_refresh,
        )

    # ── RunGateway ────────────────────────────────────────────────────────────
    gateway = RunGateway(
        graph_runner=None,  # injected per-run via start_run_with_intake
        max_concurrent=getattr(cfg, "max_concurrent_runs", 3),
    )

    # Attach helpers as convenience attributes
    gateway.intake_service = intake_service
    gateway.make_graph_runner = make_graph_runner

    logger.info(
        "Composition: gateway built (profile=%s, llm=%s, search=%s)",
        getattr(cfg, "deployment_profile", "LITE"),
        type(llm_port).__name__,
        type(search_port).__name__,
    )
    return gateway


async def start_run_with_intake_async(
    gateway: RunGateway,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    display_name: str | None = None,
    github_username: str | None = None,
    is_replay: bool = False,
    force_refresh: bool = False,
) -> str:
    """Run intake asynchronously then launch the graph in a background thread.

    **Async version** — safe to call from FastAPI async route handlers.
    Use this when you're already in an async context (e.g., FastAPI routes).

    Intake (PDF extraction + PII scrubbing) is I/O-light and fast enough to
    run synchronously. The heavy work (search, fetch, LLM calls) runs in the
    background thread started by ``RunGateway.start_run()``.
    """
    from scholarship_finder.engine.gateway import make_initial_state
    from scholarship_finder.engine.event_bus import EventBus

    # ── Intake: run async coroutine directly (we're already in an event loop) ─
    intake_result, vault = await gateway.intake_service.process(
        file_bytes, filename, mime_type, display_name=display_name
    )

    # Register vault in gateway's registry for later rehydration
    gateway.vault_registry.register(vault)

    initial_state = make_initial_state()
    # Store vault reference in state for rehydration
    initial_state.vault_reference = vault.vault_id
    # Store force_refresh flag in state for cache bypass
    initial_state.force_refresh = force_refresh

    # ── Build shared EventBus and per-run graph runner ─────────────────────────
    bus = EventBus()
    # Use extracted GitHub username from intake, fallback to manual input
    effective_github_username = intake_result.github_username or github_username
    graph_runner = gateway.make_graph_runner(
        scrubbed_text=intake_result.scrubbed_text,
        github_username=effective_github_username,
        vault=vault,
        bus=bus,
        is_replay=is_replay,
        force_refresh=force_refresh,
    )

    # ── Hand off to RunGateway (synchronous — launches background thread) ──────
    return gateway.start_run(
        initial_state,
        is_replay=is_replay,
        graph_runner=graph_runner,
        bus=bus,
    )


def start_run_with_intake(
    gateway: RunGateway,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    display_name: str | None = None,
    github_username: str | None = None,
    is_replay: bool = False,
    force_refresh: bool = False,
) -> str:
    """Run intake synchronously then launch the graph in a background thread.

    **Fully synchronous** — safe to call from non-async contexts.

    Intake (PDF extraction + PII scrubbing) is I/O-light and fast enough to
    run synchronously in a tiny temporary event loop.  The heavy work
    (search, fetch, LLM calls) runs in the background thread started by
    ``RunGateway.start_run()``.
    """
    import asyncio

    # Run the async version
    return asyncio.run(start_run_with_intake_async(
        gateway=gateway,
        file_bytes=file_bytes,
        filename=filename,
        mime_type=mime_type,
        display_name=display_name,
        github_username=github_username,
        is_replay=is_replay,
        force_refresh=force_refresh,
    ))


# ── Internal adapter builders ─────────────────────────────────────────────────

def _build_llm_port(cfg):
    """Try Groq first (more stable), fall back to Gemini, then stub.

    If both Groq and Gemini keys are available, creates a composite adapter
    that tries Groq first and falls back to Gemini on transient errors.
    """
    groq_key = getattr(cfg, "groq_api_key", None) or getattr(cfg, "openai_api_key", None)
    gemini_key = getattr(cfg, "gemini_api_key", None)

    primary_adapter = None
    failover_adapter = None

    # Build Groq/OpenAI adapter (primary - more stable)
    if groq_key:
        try:
            from scholarship_finder.adapters.llm_openai import OpenAILLMAdapter
            primary_adapter = OpenAILLMAdapter(api_key=groq_key, settings=cfg, bus=None)
            logger.info("LLM: Groq/OpenAI adapter available (primary)")
        except Exception as exc:
            logger.warning("Groq/OpenAI adapter failed to init: %s", exc)

    # Build Gemini adapter (failover) - only if quota not exhausted
    # Commented out temporarily due to quota exhaustion
    # if gemini_key:
    #     try:
    #         from scholarship_finder.adapters.llm_gemini import GeminiLLMAdapter
    #         failover_adapter = GeminiLLMAdapter(api_key=gemini_key, settings=cfg, bus=None)
    #         logger.info("LLM: Gemini adapter available (failover)")
    #     except Exception as exc:
    #         logger.warning("Gemini adapter failed to init: %s", exc)

    # If both available, create composite with runtime failover
    if primary_adapter and failover_adapter:
        logger.info("LLM: using composite adapter (Groq primary, Gemini failover)")
        return _CompositeLLMPort(primary=primary_adapter, failover=failover_adapter)

    # Otherwise use whichever is available
    if primary_adapter:
        logger.info("LLM: using Groq/OpenAI adapter only")
        return primary_adapter

    if failover_adapter:
        logger.info("LLM: using Gemini adapter only")
        return failover_adapter

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

    # If primary is not configured, use secondary as primary
    if primary is None:
        if secondary is not None:
            logger.info("Search: no primary configured, using secondary as primary")
            primary = secondary
            secondary = None
        else:
            logger.warning("Search: no primary or secondary search configured — using stub")
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
    """Build PDF adapter with optional OCR support."""
    ocr_adapter = None
    enable_ocr = getattr(cfg, "enable_ocr", True)
    ocr_threshold = getattr(cfg, "ocr_threshold_chars", 50)
    ocr_max_pages = getattr(cfg, "ocr_max_pages", 20)

    if enable_ocr:
        # Build OCR adapter if vision models are available
        vision_models = cfg.vision_models()
        if vision_models:
            try:
                from scholarship_finder.adapters.vision_ocr import VisionOCRAdapter
                # Get the LLM port for OCR (reuse existing or build new)
                llm_port = _build_llm_port(cfg)
                ocr_adapter = VisionOCRAdapter(
                    llm_port=llm_port,
                    vision_model=vision_models[0],
                    max_pages=ocr_max_pages,
                )
                logger.info("OCR: Vision OCR adapter enabled (model=%s, max_pages=%d)", vision_models[0], ocr_max_pages)
            except Exception as exc:
                logger.warning("OCR adapter init failed: %s — OCR disabled", exc)
        else:
            logger.warning("OCR enabled but no vision models configured — OCR disabled")

    try:
        from scholarship_finder.adapters.document_pdf import PDFDocumentAdapter
        return PDFDocumentAdapter(
            ocr_adapter=ocr_adapter,
            ocr_threshold=ocr_threshold,
        )
    except Exception as exc:
        logger.warning("PDF adapter init failed: %s — using stub", exc)
        return _StubDocumentPort()


def _build_profile_source(cfg):
    github_token = getattr(cfg, "github_token", None)
    if github_token:
        try:
            from scholarship_finder.adapters.profile_github import GitHubProfileAdapter
            from scholarship_finder.resilience.cache import get_github_cache
            logger.info("ProfileSource: using GitHub adapter with caching")
            return GitHubProfileAdapter(token=github_token, cache=get_github_cache())
        except Exception as exc:
            logger.warning("GitHub adapter failed: %s", exc)
    return None


def _default_quota_profiles(cfg) -> list:
    from scholarship_finder.resilience.quota import QuotaProfile
    return [
        QuotaProfile(provider="gemini", model="gemini-3.5-flash-lite", rpm=30, rpd=1500),
        QuotaProfile(provider="gemini", model="gemini-3.6-flash", rpm=15, rpd=1500),
        QuotaProfile(provider="gemini", model="gemini-3.8-flash", rpm=10, rpd=500),
        QuotaProfile(provider="groq", model="openai/gpt-oss-20b", rpm=30, rpd=1000),
        QuotaProfile(provider="groq", model="openai/gpt-oss-120b", rpm=30, rpd=1000),
        QuotaProfile(provider="openai_compat", model="openai/gpt-oss-20b", rpm=30, rpd=1000),
        QuotaProfile(provider="openai_compat", model="openai/gpt-oss-120b", rpm=30, rpd=1000),
        QuotaProfile(provider="openai", model="gpt-4o-mini", rpm=60, rpd=3000),
        QuotaProfile(provider="tavily", model="search", rpm=100, rpd=5000),
    ]


# ── Composite adapter with runtime failover ─────────────────────────────────

class _CompositeLLMPort:
    """Composite LLM port that tries primary first, falls back to secondary on errors.

    Used when both Gemini and Groq/OpenAI keys are configured.
    Transient errors (503, rate limits) trigger automatic failover.
    """
    def __init__(self, primary, failover):
        self._primary = primary
        self._failover = failover
        self._use_failover = False  # Switch state after first failover

    async def complete(self, spec):
        """Try primary, fall back to secondary on transient errors."""
        from scholarship_finder.errors import (
            TransientError, ModelUnavailableError, AdapterError,
            UnknownAdapterError, QuotaExhaustedError, RateLimitedError,
        )

        # Try primary first
        if not self._use_failover:
            try:
                return await self._primary.complete(spec)
            except (TransientError, ModelUnavailableError, UnknownAdapterError,
                    QuotaExhaustedError, RateLimitedError, AdapterError) as e:
                logger.warning("LLM primary failed (%s), switching to failover", type(e).__name__)
                self._use_failover = True
            except Exception as e:
                # Non-transient errors still fail immediately
                raise

        # Use failover
        logger.info("LLM: using failover adapter")
        return await self._failover.complete(spec)

    async def stream(self, spec):
        """Stream from primary, fall back to secondary on transient errors."""
        from scholarship_finder.errors import (
            TransientError, ModelUnavailableError, AdapterError,
            UnknownAdapterError, QuotaExhaustedError, RateLimitedError,
        )

        if not self._use_failover:
            try:
                async for chunk in self._primary.stream(spec):
                    yield chunk
                return
            except (TransientError, ModelUnavailableError, UnknownAdapterError,
                    QuotaExhaustedError, RateLimitedError, AdapterError) as e:
                logger.warning("LLM primary stream failed (%s), switching to failover", type(e).__name__)
                self._use_failover = True
            except Exception as e:
                raise

        logger.info("LLM: using failover adapter for streaming")
        async for chunk in self._failover.stream(spec):
            yield chunk

    def capabilities(self):
        # Return primary's capabilities
        return self._primary.capabilities()

    # Optional: Add bus for error reporting (backward compatible)
    def set_event_bus(self, bus):
        """Set event bus for error reporting (optional)."""
        # Store bus reference if needed for future error publishing
        pass


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
