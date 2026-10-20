# Architectural Decisions Log

Deviations from `architecture_report_autonomous_multi_agent_ai_scholarship_finder.md` and their rationale.

---

## DEC-IMPL-001: Single `models.py` for all domain models
**Change:** All 30+ domain models live in `domain/models.py` instead of per-entity files.
**Rationale:** Eliminates circular import issues with Pydantic v2 forward references across inter-related models (`Opportunity` → `EvidenceRef`, `SwarmState` → all). Discoverability is maintained via section comments.

## DEC-IMPL-002: `google-genai` SDK instead of `google-generativeai`
**Change:** Using `google-genai` (Google's unified SDK, Oct 2026) for the Gemini adapter.
**Rationale:** `google-generativeai` is deprecated. `google-genai` supports both Developer API and Vertex AI and is the recommended path per Google's own migration guide.

## DEC-IMPL-003: `trafilatura` for Tier-2 direct HTTP main-content extraction
**Change:** Using `trafilatura` instead of custom HTML parsing for Tier-2 fetches.
**Rationale:** Battle-tested, handles boilerplate removal, returns clean text. Single dependency, no custom scraper to maintain. Falls cleanly within the Fetch port boundary.

## DEC-IMPL-004: `pydantic-settings` for typed config
**Change:** `Settings` model uses `pydantic-settings` `BaseSettings`.
**Rationale:** Gives automatic env-file loading, type coercion, and validation errors at startup — catches misconfigured chains before any run starts.

## DEC-IMPL-005: Sequential subagent execution during development
**Change:** Subagents invoked one at a time instead of 5 in parallel.
**Rationale:** Free-tier quota constraint on the orchestrator model. Architectural design is unchanged.

## DEC-IMPL-006: Middleware `decorate()` as single composition entry point
**Change:** All 6 layers (Telemetry→Cache→QuotaGovernor→CircuitBreaker→Retry→Timeout) composed inside a single `decorate(fn, MiddlewareConfig)` function rather than a fluent builder chain.
**Rationale:** Simpler to test each layer independently via `MiddlewareConfig` fields set to `None`. Avoids decorator-order bugs. The config dataclass is serialisable for tracing.

## DEC-IMPL-007: `errors.TimeoutError` re-aliased as `SFTimeoutError` at import sites
**Change:** `from scholarship_finder.errors import TimeoutError as SFTimeoutError` in all resilience modules.
**Rationale:** Avoids shadowing the Python builtin `TimeoutError` while keeping the normalized error taxonomy name. No change to the public error class name.

## DEC-IMPL-008: PDF adapter falls back automatically on backend failure
**Change:** `PDFDocumentAdapter` tries PyMuPDF first, then pypdf, instead of raising immediately on primary failure.
**Rationale:** AGPL vs. BSD trade-off resolved at runtime; keeps the system operational even if one library has an install issue. `backend_used` in `DocumentResult` always reveals which backend actually ran.

## DEC-IMPL-009: LangGraph `make_graph()` returns a coroutine factory, not a compiled graph directly
**Change:** `engine/graph.py` returns `run_graph(state, bus, **kw)` instead of exposing the `CompiledGraph` object.
**Rationale:** Decouples the UI layer from LangGraph internals; the gateway only knows `await runner(state, bus)`. Also makes it straightforward to swap the graph framework without changing the gateway contract.

## DEC-IMPL-010: Agent 4 uses ONE batched reasoning call for ALL opportunities together
**Change:** A single reasoning-tier LLM call scores field alignment for all candidates in one batched prompt (up to 20), rather than one call per opportunity.
**Rationale:** Respects DEC-1 (≤4 reasoning calls/run) while still getting LLM judgment for field alignment. Truncates IDs to 8 chars in the prompt to reduce token cost.

## DEC-IMPL-011: Stub ports injected by composition.py when API keys are missing
**Change:** `_StubLLMPort`, `_StubSearchPort`, `_StubFetchPort`, `_StubDocumentPort` return empty-but-valid responses.
**Rationale:** Enables REPLAY mode and unit testing with zero live API calls. The gateway still runs the full pipeline; stubs simply return empty collections so agents degrade gracefully.

## DEC-IMPL-012: Pre-flight canary as standalone module (`preflight.py`)
**Change:** Canary logic in its own module, runnable as `python -m scholarship_finder.preflight` or imported into the Streamlit sidebar.
**Rationale:** Keeps the UI layer thin; the pre-flight logic can evolve independently. Also allows CI to run the canary without launching Streamlit.


