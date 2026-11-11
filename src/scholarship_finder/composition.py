"""Composition Root — wires all adapters, ports, and agents into a RunGateway.

This is the ONLY place where concrete adapter classes are instantiated.
All other modules depend on port interfaces only.

Usage:
    from scholarship_finder.composition import build_gateway
    gateway = build_gateway()
    run_id = await gateway.start_run(initial_state)
"""
from __future__ import annotations

import asyncio
import logging

from scholarship_finder.config import get_settings
from scholarship_finder.domain.models import CandidateProfile
from scholarship_finder.engine.gateway import RunGateway

logger = logging.getLogger(__name__)


def build_gateway(settings=None) -> RunGateway:
    """Build a fully-wired RunGateway from configuration.

    Adapters are constructed lazily and fall back gracefully when API keys
    are missing (stub mode keeps tests and demos working offline).
    """
    cfg = settings or get_settings()

    # ── LLM: providers + router ──────────────────────────────────────────────
    llm_port, router = _build_llm(cfg)

    # ── Search adapters ───────────────────────────────────────────────────────
    search_port, secondary_search, tavily_adapter = _build_search_ports(cfg)

    # ── Fetch adapter (uses Tavily Extract tier when available) ──────────────
    fetch_port = _build_fetch_port(cfg, tavily_adapter)

    # ── Document adapter (OCR through the router's vision tier) ───────────────
    document_port = _build_document_port(cfg, llm_port)

    # ── Profile source adapter ────────────────────────────────────────────────
    profile_source = _build_profile_source(cfg)

    # ── Agents ────────────────────────────────────────────────────────────────
    from scholarship_finder.agents.profile_enricher import ProfileEnricherAgent
    from scholarship_finder.agents.opportunity_scout import OpportunityScoutAgent
    from scholarship_finder.agents.source_auditor import SourceAuditorAgent
    from scholarship_finder.agents.ranking_strategist import RankingStrategistAgent
    from scholarship_finder.agents.application_strategist import ApplicationStrategistAgent
    from scholarship_finder.agents.gap_advisor import GapAdvisorAgent

    profile_enricher = ProfileEnricherAgent(llm_port, profile_source)
    opportunity_scout = OpportunityScoutAgent(llm_port, search_port, secondary_search)
    source_auditor = SourceAuditorAgent(llm_port, fetch_port)
    ranking_strategist = RankingStrategistAgent(llm_port)
    application_strategist = ApplicationStrategistAgent(llm_port)
    gap_advisor = GapAdvisorAgent(llm_port)

    # ── Intake service ─────────────────────────────────────────────────────────
    from scholarship_finder.agents.intake import IntakeService
    intake_service = IntakeService(
        document_port=document_port,
        vault_ttl_seconds=getattr(cfg, "pii_vault_ttl_seconds", 900),
    )

    # ── Per-run graph runner factory ───────────────────────────────────────────
    def make_graph_runner(
        scrubbed_text: str | None = None,
        github_username: str | None = None,
        vault=None,
        bus=None,
        is_replay: bool = False,
        force_refresh: bool = False,
    ):
        from scholarship_finder.engine.graph import make_graph

        return make_graph(
            profile_enricher_agent=profile_enricher,
            opportunity_scout_agent=opportunity_scout,
            source_auditor_agent=source_auditor,
            ranking_strategist_agent=ranking_strategist,
            gap_advisor_agent=gap_advisor,
            application_strategist_agent=application_strategist,
            bus=bus,
            scrubbed_text=scrubbed_text,
            github_username=github_username,
            vault=vault,
            is_replay=is_replay,
            target_verified=cfg.target_verified_opportunities,
        )

    # ── Per-loop adapter cleanup (called at the end of each run's loop) ──────
    async def aclose_adapters() -> None:
        for obj in (router, fetch_port, tavily_adapter):
            close = getattr(obj, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    logger.debug("adapter cleanup failed for %s", type(obj).__name__)

    # ── RunGateway ────────────────────────────────────────────────────────────
    gateway = RunGateway(
        graph_runner=None,
        max_concurrent=getattr(cfg, "max_concurrent_runs", 3),
    )

    gateway.intake_service = intake_service
    gateway.make_graph_runner = make_graph_runner
    gateway.application_strategist = application_strategist
    gateway.aclose_adapters = aclose_adapters
    gateway.llm_port = llm_port
    gateway.llm_router = router

    logger.info(
        "Composition: gateway built (profile=%s, providers=%s, search=%s)",
        getattr(cfg, "deployment_profile", "LITE"),
        sorted(getattr(router, "_providers", {}).keys()) if router else ["stub"],
        type(search_port).__name__,
    )
    return gateway


async def start_run_with_intake_async(
    gateway: RunGateway,
    files: list[tuple[bytes, str, str]],
    form_profile: CandidateProfile | None = None,
    display_name: str | None = None,
    github_username: str | None = None,
    is_replay: bool = False,
) -> str:
    """Run multi-file intake then launch the graph in a background thread.

    ``files`` is a list of (bytes, filename, label) — label in
    RESUME|TRANSCRIPT|OTHER.
    """
    from scholarship_finder.engine.gateway import make_initial_state
    from scholarship_finder.engine.event_bus import EventBus

    bundle, vault = await gateway.intake_service.process_documents(
        files,
        form_profile=form_profile,
        display_name=display_name,
        github_username=github_username,
    )
    gateway.vault_registry.register(vault)

    initial_state = make_initial_state()
    initial_state.vault_id = vault.vault_id
    initial_state.intake = bundle

    bus = EventBus()
    graph_runner = gateway.make_graph_runner(
        scrubbed_text="",
        github_username=bundle.github_username,
        vault=vault,
        bus=bus,
        is_replay=is_replay,
    )

    return gateway.start_run(
        initial_state,
        is_replay=is_replay,
        graph_runner=graph_runner,
        bus=bus,
    )


def start_run_with_intake(
    gateway: RunGateway,
    files: list[tuple[bytes, str, str]],
    form_profile: CandidateProfile | None = None,
    display_name: str | None = None,
    github_username: str | None = None,
    is_replay: bool = False,
) -> str:
    """Synchronous wrapper around :func:`start_run_with_intake_async`."""
    return asyncio.run(start_run_with_intake_async(
        gateway=gateway,
        files=files,
        form_profile=form_profile,
        display_name=display_name,
        github_username=github_username,
        is_replay=is_replay,
    ))


# ── Internal adapter builders ─────────────────────────────────────────────────

def _build_llm(cfg):
    """Build providers from configured keys and the routing LLMRouter.

    Returns ``(llm_port, router_or_None)``. ``llm_port`` is the router when at
    least one provider exists, else a stub.
    """
    from scholarship_finder.adapters.llm_router import LLMRouter
    from scholarship_finder.resilience.breaker import get_breaker_registry
    from scholarship_finder.resilience.quota import ModelLimiter

    providers: dict[str, object] = {}

    if getattr(cfg, "groq_api_key", ""):
        try:
            from scholarship_finder.adapters.llm_openai import OpenAICompatProvider
            providers["groq"] = OpenAICompatProvider(
                name="groq",
                api_key=cfg.groq_api_key,
                base_url=cfg.groq_api_base_url,
            )
            logger.info("LLM: Groq provider configured")
        except Exception as exc:
            logger.warning("Groq provider init failed: %s", exc)

    if getattr(cfg, "openai_api_key", ""):
        try:
            from scholarship_finder.adapters.llm_openai import OpenAICompatProvider
            providers["openai"] = OpenAICompatProvider(
                name="openai",
                api_key=cfg.openai_api_key,
                base_url=cfg.openai_api_base_url,
            )
            logger.info("LLM: OpenAI-compatible provider configured")
        except Exception as exc:
            logger.warning("OpenAI provider init failed: %s", exc)

    if getattr(cfg, "gemini_api_key", ""):
        try:
            from scholarship_finder.adapters.llm_gemini import GeminiProvider
            providers["gemini"] = GeminiProvider(api_key=cfg.gemini_api_key)
            logger.info("LLM: Gemini provider configured")
        except Exception as exc:
            logger.warning("Gemini provider init failed: %s", exc)

    if getattr(cfg, "local_llm_enabled", False):
        try:
            from scholarship_finder.adapters.llm_local import LocalLlamaProvider
            providers["local"] = LocalLlamaProvider(
                base_url=cfg.local_llm_base_url)
            logger.info("LLM: local provider configured (%s)",
                        cfg.local_llm_base_url)
        except Exception as exc:
            logger.warning("Local provider init failed: %s", exc)

    if not providers:
        logger.warning("LLM: no API keys configured — using stub (test/demo only)")
        return _StubLLMPort(), None

    router = LLMRouter(
        settings=cfg,
        providers=providers,
        limiter=ModelLimiter(),
        breakers=get_breaker_registry(),
    )
    return router, router


def _build_search_ports(cfg):
    tavily_adapter = None
    primary = None
    if getattr(cfg, "tavily_api_key", ""):
        try:
            from scholarship_finder.adapters.search_tavily import TavilySearchAdapter
            tavily_adapter = TavilySearchAdapter(api_key=cfg.tavily_api_key)
            primary = tavily_adapter
            logger.info("Search: primary=Tavily")
        except Exception as exc:
            logger.warning("Tavily adapter failed: %s", exc)

    secondary = None
    secondary_provider = getattr(cfg, "search_secondary_provider", "jina")
    if str(secondary_provider).endswith("serper"):
        serper_key = getattr(cfg, "serper_api_key", "")
        if serper_key:
            try:
                from scholarship_finder.adapters.search_jina import SerperAdapter
                secondary = SerperAdapter(api_key=serper_key)
                logger.info("Search: secondary=Serper")
            except Exception as exc:
                logger.warning("Serper adapter failed: %s", exc)
    else:
        try:
            from scholarship_finder.adapters.search_jina import JinaSearchAdapter
            secondary = JinaSearchAdapter(api_key=getattr(cfg, "jina_api_key", ""))
            logger.info("Search: secondary=Jina")
        except Exception as exc:
            logger.warning("Jina adapter failed: %s", exc)

    if primary is None:
        if secondary is not None:
            logger.info("Search: no primary configured, using secondary as primary")
            primary = secondary
            secondary = None
        else:
            logger.warning("Search: no providers configured — using stub")
            primary = _StubSearchPort()

    return primary, secondary, tavily_adapter


def _build_fetch_port(cfg, tavily_adapter):
    try:
        from scholarship_finder.adapters.fetch_chain import FetchChain
        logger.info("Fetch: using FetchChain")
        return FetchChain(settings=cfg, tavily=tavily_adapter)
    except Exception as exc:
        logger.warning("FetchChain init failed: %s — using stub", exc)
        return _StubFetchPort()


def _build_document_port(cfg, llm_port):
    """Build PDF adapter with optional OCR through the LLM port's vision tier."""
    ocr_adapter = None
    if getattr(cfg, "enable_ocr", True):
        vision_models = cfg.vision_models() if hasattr(cfg, "vision_models") else []
        if vision_models:
            try:
                from scholarship_finder.adapters.vision_ocr import VisionOCRAdapter
                ocr_adapter = VisionOCRAdapter(
                    llm_port=llm_port,
                    vision_model=vision_models[0],
                    max_pages=getattr(cfg, "ocr_max_pages", 10),
                )
                logger.info("OCR: enabled (model=%s)", vision_models[0])
            except Exception as exc:
                logger.warning("OCR adapter init failed: %s — OCR disabled", exc)

    try:
        from scholarship_finder.adapters.document_pdf import PDFDocumentAdapter
        return PDFDocumentAdapter(
            ocr_adapter=ocr_adapter,
            ocr_threshold=getattr(cfg, "ocr_threshold_chars", 50),
        )
    except Exception as exc:
        logger.warning("PDF adapter init failed: %s — using stub", exc)
        return _StubDocumentPort()


def _build_profile_source(cfg):
    github_token = getattr(cfg, "github_token", "")
    if github_token:
        try:
            from scholarship_finder.adapters.profile_github import GitHubProfileAdapter
            from scholarship_finder.resilience.cache import get_github_cache
            logger.info("ProfileSource: using GitHub adapter with caching")
            return GitHubProfileAdapter(token=github_token, cache=get_github_cache())
        except Exception as exc:
            logger.warning("GitHub adapter failed: %s", exc)
    return None


# ── Stub ports (used when no API keys are configured) ─────────────────────────

class _StubLLMPort:
    """No-op LLM — returns empty JSON. For tests/demos without keys."""

    async def complete(self, spec):
        from scholarship_finder.ports.llm import LLMResult, LLMUsage
        return LLMResult(content="{}", usage=LLMUsage(), provider="stub", tier=spec.tier)

    async def complete_json(self, spec):
        result = await self.complete(spec)
        result.parsed = {}
        return result

    def available(self) -> bool:
        return False


class _StubSearchPort:
    async def search(self, query):
        from scholarship_finder.ports.search import SearchResult
        return SearchResult(items=[], provider="stub", query_text=query.text)

    def capabilities(self):
        return {"returns_page_content": False, "provider": "stub"}


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
