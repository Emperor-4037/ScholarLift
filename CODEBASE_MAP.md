# Autonomous Multi-Agent AI Scholarship Finder: Exhaustive Codebase & Pipeline Map

> **Target Audience**: Autonomous AI Agents, Code Analysis Engines, and Systems Engineers.  
> **Purpose**: Complete structural, behavioral, dependency, and failure-mode map of the Scholarship Finder codebase.  
> **Guiding Principle**: Any agent reading this document alone must possess 100% operational understanding of every component, data contract, runtime invariant, and potential point of failure without needing to inspect raw source code.

---

## 1. Executive Overview & System Architecture

### 1.1 Purpose & Mission
The **Autonomous Multi-Agent AI Scholarship Finder** is an enterprise-grade autonomous pipeline designed to discover, audit, verify, rank, and draft customized application packages (Statement of Purpose and checklist) for graduate scholarships and academic programs worldwide.

### 1.2 Core Architectural Pattern: Hexagonal (Ports & Adapters)
The system strictly decouples business domain logic from external vendors (LLM providers, search engines, web scraping, document parsers, and databases):
* **Domain Layer (`domain/`)**: Pure, deterministic, side-effect-free business logic (e.g. GPA conversion formulas, URL normalization, cryptographic text comparison, tri-state boolean logic, mathematical scoring, PII redaction). Zero external dependencies.
* **Ports Layer (`ports/`)**: Abstract Python protocols (`typing.Protocol`) defining the interfaces required by the application (LLM, Web Search, Web Fetch, Document Extraction, Profile Signals).
* **Adapters Layer (`adapters/`)**: Concrete implementations communicating with external services (Gemini via `google-genai`, Groq/OpenAI via `openai`, Tavily API, Jina Reader/Search, GitHub REST API, PyMuPDF/pypdf).
* **Resilience Stack (`resilience/`)**: 6-layer decorator chain wrapping every adapter call (Telemetry → Cache → QuotaGovernor → CircuitBreaker → Retry → Timeout).
* **Engine Layer (`engine/`)**: LangGraph StateGraph orchestration, bounded ring-buffer EventBus, and background thread execution gateway (`RunGateway`).
* **Agents Layer (`agents/`)**: 5 autonomous agents plus a deterministic intake pre-processor that manipulate a shared typed blackboard (`SwarmState`).
* **API & Web Layer (`api/`, `web/`)**: FastAPI HTTP endpoints, Server-Sent Events (SSE) streaming, Jinja2 templating, and vanilla JS client.

```
       ┌────────────────────────────────────────────────────────────┐
       │              Web Client (Vanilla JS / SSE)                 │
       └─────────────────────────────┬──────────────────────────────┘
                                     │ HTTP / SSE
       ┌─────────────────────────────▼──────────────────────────────┐
       │              FastAPI Application (`api/`)                  │
       └─────────────────────────────┬──────────────────────────────┘
                                     │ start_run_with_intake_async()
       ┌─────────────────────────────▼──────────────────────────────┐
       │           Intake Service (PDF extract + PII scrub)         │
       └─────────────────────────────┬──────────────────────────────┘
                                     │ SwarmState + PIIVault
       ┌─────────────────────────────▼──────────────────────────────┐
       │     RunGateway (Dedicated OS Thread + asyncio EventLoop)   │
       │  ┌──────────────────────────────────────────────────────┐  │
       │  │ LangGraph StateGraph Orchestration (`engine/graph.py`)│  │
       │  │                                                      │  │
       │  │   [ProfileEnricher] ──> [OpportunityScout]           │  │
       │  │                               │                      │  │
       │  │                        (R1: candidates >= 10?)       │  │
       │  │                               │                      │  │
       │  │   [SourceAuditor]   <─────────┴─────────> [Retry]    │  │
       │  │          │                                           │  │
       │  │   (R2: opps >= 1?)                                   │  │
       │  │          │                                           │  │
       │  │   [RankingStrategist] ──> (PHASE_A_READY Event)      │  │
       │  │          │                                           │  │
       │  │   [ApplicationStrategist]                            │  │
       │  │          │                                           │  │
       │  │   [CompleteEnd] / [DegradedEnd]                      │  │
       │  └──────────────────────────┬───────────────────────────┘  │
       └─────────────────────────────┼──────────────────────────────┘
                                     │ Decorated Port Invocations
       ┌─────────────────────────────▼──────────────────────────────┐
       │   6-Layer Resilience Stack (`resilience/middleware.py`)    │
       │   [Telemetry] -> [Cache] -> [Quota] -> [Breaker] ->        │
       │   [Retry] -> [Timeout]                                     │
       └─────────────────────────────┬──────────────────────────────┘
                                     │ Abstract Port Protocols
       ┌─────────────────────────────▼──────────────────────────────┐
       │    Adapters (Gemini, Groq, Tavily, Jina, Playwright, FitZ) │
       └────────────────────────────────────────────────────────────┘
```

---

## 2. Hard System Invariants & Non-Negotiables

Any autonomous modification must strictly uphold these invariants:

| # | Invariant Rule | Exact Enforcement Mechanism | Consequence of Violation |
|---|----------------|-----------------------------|--------------------------|
| **INV-1** | **No Model IDs in Code** | Ordered model chain strings in `.env` parsed by `config.py` | Hard-coded vendor locks, broken test fixtures |
| **INV-2** | **DEC-1: $\le 4$ Reasoning Calls/Run** | Enforced by `QuotaGovernor` via token counter on `tier="reasoning"` | Quota exhaustion, immediate `QuotaExhaustedError` |
| **INV-3** | **Zero PII Outside Intake** | `PIIScrubber` sanitizes text into `PIIVault` before graph launch | Data leakage to external LLM/Search logs |
| **INV-4** | **Stage-1 Anti-Hallucination** | `verify_span()` verifies exact verbatim evidence substrings in page text | Fabricated scholarship attributes enter state |
| **INV-5** | **Stage-2 Anti-Hallucination** | `verify_sop()` enforces fact-pack referential integrity, numeric grounding, proper nouns | Invented qualifications or fake GPA in SOP |
| **INV-6** | **Phase A Hard Deadline** | `asyncio.wait_for(..., timeout=phase_a_hard_s + phase_b_cap_s)` | Zombie processes, pipeline run timeouts |
| **INV-7** | **Typed Monotonic State Merges** | `domain/reducers.py` enforces forward-only phases and `EVIDENCED` status retention | Overwriting verified facts with unverified web scrapers |
| **INV-8** | **Thread Isolation** | Pipeline runs in daemon `threading.Thread` with dedicated `asyncio` event loop | Event loop deadlock with FastAPI main loop |
| **INV-9** | **Vendor Exception Boundary** | All adapters map SDK exceptions into `errors.py` hierarchy | Unhandled vendor exceptions breaking retry/breaker logic |

---

## 3. Global Concurrency & Threading Model

```mermaid
sequenceDiagram
    autonumber
    actor User as Web Client (JS)
    participant API as FastAPI Main Thread
    participant GW as RunGateway (Shared)
    participant Loop as Dedicated Background OS Thread (asyncio Loop)
    participant Bus as EventBus (Thread-Safe Ring Buffer)
    participant Graph as LangGraph Engine

    User->>API: POST /api/upload (File + Metadata)
    API->>API: IntakeService.process() (Scrub PII -> PIIVault)
    API->>GW: start_run(initial_state, bus, graph_runner)
    GW->>Bus: publish_sync(RUN_STARTED)
    GW->>Loop: Spawn threading.Thread(daemon=True, target=_run_in_thread)
    Loop->>Loop: asyncio.new_event_loop()
    GW-->>API: Returns run_id (Immediate)
    API-->>User: HTTP 200 { run_id: "..." }

    par SSE Streaming
        User->>API: GET /api/events/{run_id}
        loop Every 500ms
            API->>Bus: since(cursor) [threading.Lock guarded]
            Bus-->>API: New Event batch
            API-->>User: SSE Data Frame
        end
    and Pipeline Execution
        Loop->>Graph: graph.ainvoke(initial_state)
        Graph->>Bus: publish(NODE_STARTED, PROGRESS, etc.)
        Graph->>Graph: Run A1 -> A2 -> A3 -> A4 -> A5
        Graph-->>Loop: Final SwarmState
        Loop->>Bus: publish_sync(RUN_COMPLETED)
    end
```

### Critical Concurrency Details:
1. **FastAPI Loop vs. Background Loop**: The web server runs on the Uvicorn event loop in the process main thread. The agent graph runs on an entirely separate OS thread with a fresh, private `asyncio` loop created via `asyncio.new_event_loop()`.
2. **Thread-Safe EventBus**: The `EventBus` uses standard `threading.Lock` (NOT `asyncio.Lock`). Methods `publish()`, `publish_sync()`, and `since()` can be invoked safely from any thread without risking thread contention or deadlocks.
3. **Run Isolation**: Multiple users can execute runs concurrently up to `MAX_CONCURRENT_RUNS` (default: 3). If concurrent runs exceed this limit, `RunGateway.start_run()` immediately raises `RuntimeError`.

---

## 4. End-to-End Execution Trace

### Phase 0: Intake & Privacy Vault Initialization
1. Client POSTs PDF/Markdown resume to `/api/upload`.
2. `IntakeService.process()`:
   * Validates file size ($\le 10$ MB) and MIME type.
   * Extracts text using `PDFDocumentAdapter` (`pymupdf` primary, `pypdf` fallback). If character density is low ($< 50$ chars/page) and OCR is enabled, invokes `VisionOCRAdapter` (Gemini Vision).
   * Regex-identifies candidate name, email, phone, Aadhaar, SSN, Passport, and GitHub profile URLs.
   * Stores real identities in `PIIVault` with in-memory TTL (default: 900 seconds / 15 minutes).
   * Replaces raw PII with deterministic tokens: `[CANDIDATE_NAME]`, `[REDACTED_EMAIL]`, `[REDACTED_PHONE]`, `[REDACTED_ID]`.
   * Returns `IntakeResult` and instantiates `SwarmState`.
3. `RunGateway.vault_registry.register(vault)` caches the vault instance in memory for end-of-run SOP rehydration.

### Phase A: Discovery & Verification ($\le 75$ seconds target)
1. **Node 1: `profile_enricher` (Agent 1)**
   * Calls fast-tier LLM to convert scrubbed resume text into structured `CandidateProfile`.
   * Simultaneously fires asynchronous GitHub REST API query (`ProfileSourcePort`) if username was found or provided.
   * Runs deterministic GPA normalization (`normalize_gpa`) mapping Indian (10.0), Percentage (100.0), or standard systems to US 4.0 scale and German Bavarian scale.
   * Merges GitHub signals (repositories, primary languages, topic tags) and computes boolean profile completeness flags.
   * Updates `SwarmState.profile` via `reduce_profile()`.
2. **Node 2: `opportunity_scout` (Agent 2)**
   * Calls fast-tier LLM with `_QUERY_PLAN_PROMPT` to synthesize 3–5 targeted queries spanning `program`, `scholarship`, and `funder` intents.
   * Injects domain exclusions (`_EXCLUDE_DOMAINS`) removing known low-value aggregators.
   * Dispatches parallel web searches via `TavilySearchAdapter` (or secondary fallback `JinaSearchAdapter` / `SerperAdapter`).
   * Canonicalizes URLs (`canonicalize_url`), strips tracking query params, classifies domain trust tiers (`classify_domain`), and deduplicates by canonical URL.
   * Updates `SwarmState.candidates` via `reduce_candidates()`.
3. **Predicate `R1`: Candidate Sufficiency Check**
   * If `candidates >= 10` or `retry_count >= max_retries`: advances to `source_auditor`.
   * Otherwise: routes to `node_retry_scout`, increments `retry_count`, logs `RETRY_TRIGGERED`, and re-invokes scout with alternative query variations.
4. **Node 3: `source_auditor` (Agent 3)**
   * Executes wave-pipelined fetching (waves of 10 URLs) via `FetchChain` (Tier 0 Cache → Tier 1 Jina Reader → Tier 2 Direct HTTP + Trafilatura → Tier 3 Playwright Headless Browser).
   * Discards bot-traps, captchas, and JS-shell pages. Follows links on directory pages up to depth 1.
   * Batches pages (5 pages per LLM fast-tier completion call) to extract program name, degree level, country, institution, funding levels, and deadlines.
   * **Stage-1 Anti-Hallucination**: Calls `verify_span()` on every extracted attribute. The attribute's evidence string must exist as an exact normalized substring in the fetched page text. If verification fails, attribute status is demoted to `UNVERIFIED`.
   * Filters out generic scholarship listing portals (`_is_listing_page`).
   * Ceases processing early once $\ge 20$ existence-verified opportunities are obtained.
   * Updates `SwarmState.opportunities` via `reduce_opportunities()`.
5. **Predicate `R2`: Opportunity Viability Check**
   * If `len(opportunities) >= 1`: advances to `ranking_strategist`.
   * If `0`: routes to `degraded_end`, emits `RUN_DEGRADED`, marks outcome `PARTIAL`.
6. **Node 4: `ranking_strategist` (Agent 4)**
   * Deterministically evaluates tri-state eligibility rules (`evaluate_gpa_minimum`, `evaluate_degree_field`, `evaluate_degree_level`, etc.) yielding `MET`, `UNMET`, or `UNKNOWN`.
   * Executes **EXACTLY 1 batched reasoning-tier LLM call** (`LLMTier.REASONING`) to calculate field alignment (0.0–1.0) and produce concise match rationales.
   * Computes composite score ($0.0 - 100.0$) using weighted algorithm: Academic Fit (25%), Field Alignment (25%), Funding Strength (20%), Feasibility (20%), Evidence Confidence (10%).
   * Hard Rule Cap: Any blocking `UNMET` eligibility rule caps the composite score at $25.0$.
   * Selects Top 5 opportunities with URL-based diversity deduplication.
   * Emits `PHASE_A_READY` event over `EventBus` notifying the UI that discovery cards can be displayed.

### Phase B: Application Strategy ($\le 45$ seconds target)
1. **Predicate `R3`: SOP Needed Check**
   * If `top_5` and `profile` exist: advances to `application_strategist`.
   * Otherwise: advances to `complete_end`.
2. **Node 5: `application_strategist` (Agent 5)**
   * Takes the #1 ranked opportunity from Top 5.
   * Constructs an **Allowed-Fact Pack**: a strict key-value ledger (`FACT_001`, `FACT_002`, ...) containing only verified candidate facts and audited opportunity requirements.
   * Calls fast-tier LLM to generate Statement of Purpose (350–450 words) using ONLY allowed facts.
   * **Stage-2 Anti-Hallucination Verification**:
     1. *Referential Integrity Guard*: Verifies all cited `FACT_XXX` tokens exist in the allowed pack.
     2. *Numeric Token Guard*: Regex extracts all numbers/percentages/dates/currencies in each sentence; verifies every number appears in the cited facts.
     3. *Proper-Noun Guard*: Checks all capitalized multi-word phrases against allowed facts and academic stop-words.
   * *Repair & Strip Loop*: Any failing sentence undergoes 1 targeted fast-tier repair prompt. If still failing, the sentence is stripped from the SOP entirely.
   * *Name Rehydration*: Replaces `[CANDIDATE_NAME]` tokens with real names locally from `PIIVault` (real identity never seen by LLM).
   * Generates a concrete execution checklist (deadlines, translation requirements, recommendations).
   * Streams SOP text chunks (`SOP_CHUNK` events) to UI.
3. **Node 6: `complete_end`**
   * Sets `RunMeta.outcome = COMPLETE`, `RunPhase = COMPLETED`.
   * Emits terminal event `RUN_COMPLETED`. Background thread terminates cleanly.

---

## 5. Comprehensive Component-by-Component Map

```mermaid
graph TD
    subgraph Config & Composition
        CFG[config.py]
        COMP[composition.py]
        ERR[errors.py]
        PRE[preflight.py]
    end

    subgraph Domain Core
        MOD[domain/models.py]
        GPA[domain/gpa.py]
        URL[domain/url.py]
        TRU[domain/trust.py]
        PII[domain/pii.py]
        RED[domain/reducers.py]
        SPAN[domain/span_verifier.py]
        CLAIM[domain/claim_verifier.py]
        TRI[domain/tri_state.py]
        SCOR[domain/scoring.py]
        TIPS[domain/enrichment_tips.py]
    end

    subgraph Ports & Adapters
        P_DOC[ports/document.py] --> A_DOC[adapters/document_pdf.py]
        P_DOC --> A_OCR[adapters/vision_ocr.py]
        P_FET[ports/fetch.py] --> A_FET[adapters/fetch_chain.py]
        P_LLM[ports/llm.py] --> A_GEM[adapters/llm_gemini.py]
        P_LLM --> A_OAI[adapters/llm_openai.py]
        P_PRF[ports/profile_source.py] --> A_GH[adapters/profile_github.py]
        P_SRCH[ports/search.py] --> A_TAV[adapters/search_tavily.py]
        P_SRCH --> A_JIN[adapters/search_jina.py]
    end

    subgraph Resilience Stack
        RES_MID[resilience/middleware.py]
        RES_BRK[resilience/breaker.py]
        RES_QTA[resilience/quota.py]
        RES_RET[resilience/retry.py]
        RES_CCH[resilience/cache.py]
        RES_TEL[resilience/telemetry.py]
        RES_TMO[resilience/timeout.py]
    end

    subgraph Agents & Engine
        AG_INT[agents/intake.py]
        AG_PRF[agents/profile_enricher.py]
        AG_SCT[agents/opportunity_scout.py]
        AG_AUD[agents/source_auditor.py]
        AG_RNK[agents/ranking_strategist.py]
        AG_APP[agents/application_strategist.py]
        ENG_BUS[engine/event_bus.py]
        ENG_GTW[engine/gateway.py]
        ENG_GRP[engine/graph.py]
    end

    subgraph API & Web
        API_M[api/main.py]
        API_R[api/routes.py]
        API_S[api/sse.py]
        WEB_A[web/static/js/app.js]
    end

    COMP --> Ports & Adapters
    COMP --> Resilience Stack
    COMP --> Agents & Engine
    ENG_GTW --> ENG_GRP
    ENG_GRP --> Agents & Engine
    API_R --> ENG_GTW
```

---

### 5.1 Root System & Configuration Modules

#### `src/scholarship_finder/config.py`
* **Purpose**: Pydantic v2 `BaseSettings` object reading `.env`. Single source of configuration truth.
* **Responsibilities**:
  * Loads and parses model tier chains (`TIER_FAST_CHAIN`, `TIER_REASONING_CHAIN`, `TIER_VISION_CHAIN`).
  * Validates deployment profiles (`LITE` vs `FULL`).
  * Emits runtime warnings if critical API keys (`GEMINI_API_KEY`, `TAVILY_API_KEY`) are missing.
* **Key Classes/Functions**:
  * `Settings`: Main configuration model.
  * `get_settings()`: `@lru_cache(maxsize=1)` singleton provider.
  * `fast_models()`, `reasoning_models()`, `vision_models()`: Helper parsers returning list of model IDs.
* **Dependencies**: `pydantic_settings`.
* **Potential Problems & Edge Cases**:
  * *Trailing/Leading Spaces in `.env`*: Chain parsing cleans entries, but malformed model names cause runtime 404s.
  * *Missing Keys*: Will default to empty strings without raising errors at boot, which cascades to stub ports unless caught.
  * *Cached Settings*: Calling `get_settings()` caches the object. Dynamic runtime modification of environment variables will not take effect unless cache is cleared.

#### `src/scholarship_finder/composition.py`
* **Purpose**: Dependency Injection (DI) Root. Sole location where concrete adapter classes are instantiated.
* **Responsibilities**:
  * Wires concrete adapters (`GeminiLLMAdapter`, `OpenAILLMAdapter`, `TavilySearchAdapter`, `FetchChain`, `PDFDocumentAdapter`) to port interfaces.
  * Constructs `_CompositeLLMPort` providing non-sticky automatic failover from Gemini to Groq/OpenAI on transient errors.
  * Instantiates fallback stub ports (`_StubLLMPort`, `_StubSearchPort`, `_StubFetchPort`, `_StubDocumentPort`) when API keys are absent.
  * Provides `start_run_with_intake_async()` and synchronous `start_run_with_intake()`.
* **Key Classes/Functions**:
  * `build_gateway()`: Factory that wires all agents, ports, resilience stack, and gateway.
  * `_CompositeLLMPort`: Complete implementation with primary and failover fallbacks.
  * `start_run_with_intake_async()`: Async entry point for FastAPI routes.
* **Dependencies**: All adapters, ports, agents, `RunGateway`, `EventBus`.
* **Potential Problems & Edge Cases**:
  * *Circular References*: If any port or agent imports `composition.py`, an import cycle occurs.
  * *Failover Adapter Mismatch*: `_CompositeLLMPort` completes fallback calls with Groq, which emits `<think>` tags on reasoning models; if not stripped downstream, JSON parsers fail.

#### `src/scholarship_finder/errors.py`
* **Purpose**: Application-wide normalized error taxonomy.
* **Responsibilities**:
  * Prevents vendor SDK exceptions (`google.genai.errors`, `openai.APIStatusError`, `httpx.HTTPStatusError`) from leaking across port boundaries.
  * Classifies errors into actionable categories with predefined retry eligibility.
* **Key Classes/Hierarchy**:
  * `ScholarshipFinderError`
    * `DomainError`
    * `AdapterError` (has `retry_eligible: bool`)
      * `RateLimitedError` (tracks `scope: RateLimitScope`, `retry_after: float`)
      * `QuotaExhaustedError` (non-retryable; trips breaker to `KNOWN_EXHAUSTED`)
      * `TimeoutError`
      * `TransientError` (retryable: 5xx, network resets)
      * `SchemaViolationError` (retryable once for repair)
      * `ContextTooLongError`
      * `ContentBlockedError` (safety filters)
      * `ModelUnavailableError` (404/deprecated)
      * `AuthError` (401/403 credentials invalid)
      * `NotFoundError` (HTTP 404)
      * `BlockedError` (Cloudflare/captcha anti-bot)
      * `UnknownAdapterError`
* **Potential Problems & Edge Cases**:
  * *Unmapped Exceptions*: If an adapter catches an unknown exception and fails to wrap it in `UnknownAdapterError`, raw SDK types leak into the resilience stack, bypassing retry logic.

#### `src/scholarship_finder/preflight.py`
* **Purpose**: Standalone CLI canary probe (`python -m scholarship_finder.preflight` or `scholarship-preflight`).
* **Responsibilities**:
  * Concurrently executes live smoke-probes against Gemini, Groq, Tavily, Jina, FetchChain, and GitHub.
  * Returns structured `PreflightReport` with latency timings and pass/fail indicators.
* **Potential Problems & Edge Cases**:
  * *Terminal Encoding*: Windows PowerShell terminals without UTF-8 support will crash on emoji output (`UnicodeEncodeError`), handled by explicit stdout reconfiguration.

#### `src/scholarship_finder/cli.py`
* **Purpose**: CLI executable entrypoint (`scholarship-run`).
* **Responsibilities**: Runs `uvicorn.run("scholarship_finder.api.main:app", host=..., port=..., reload=...)`.

---

### 5.2 Pure Domain Layer (`src/scholarship_finder/domain/`)

#### `src/scholarship_finder/domain/models.py`
* **Purpose**: Single monolithic Pydantic v2 data model file containing all domain models and enums.
* **Why Monolithic**: Intentionally kept in one file to prevent Pydantic v2 forward-reference resolution issues and circular import deadlocks.
* **Core Enums**:
  * `RunOutcome`: `IN_PROGRESS`, `COMPLETE`, `DEGRADED`, `PARTIAL`, `FAILED_SAFE`, `CANCELLED`
  * `RunPhase`: `INTAKE`, `PHASE_A`, `PHASE_B`, `COMPLETED`
  * `TrustTier`: `OFFICIAL_INSTITUTION`, `RECOGNIZED_FUNDER`, `AGGREGATOR`, `UNKNOWN`
  * `FieldStatus`: `EVIDENCED`, `UNVERIFIED`
  * `FundingLevel`: `FULL_TUITION_STIPEND`, `FULL_TUITION`, `PARTIAL`, `NONE`, `UNKNOWN`
* **Core Models**:
  * `EvidenceRef`: Provenance anchor pointing to verbatim page text (`span`, `offset`, `location`). Span length must be 15–400 chars.
  * `NormalizedGPA`: Normalized value, source scale, method, confidence, and disclaimers.
  * `CandidateProfile`: Parsed resume (education, skills, projects, preferences, language tests, github signals).
  * `Candidate`: Raw URL discovered by scout with domain and trust tier.
  * `Opportunity`: Verified opportunity with fields, deadlines, funding, requirements, and verification score.
  * `EligibilityAssessment`: Output of ranking agent with tri-state rules, actionable gaps, component scores, composite score, and LLM rationale.
  * `ApplicationPackage`: Final deliverable (SOP sentences, claim ledger, checklist).
  * `SwarmState`: Central shared blackboard across all agents.
* **Potential Problems & Edge Cases**:
  * *Evidence Span Length Validator*: Spans $< 15$ or $> 400$ chars raise a Pydantic `ValidationError`. Adapters must ensure spans are checked before instantiation.
  * *Mutable Defaults*: Lists and dicts use `default_factory`. Never use direct mutable defaults.

#### `src/scholarship_finder/domain/gpa.py`
* **Purpose**: Pure deterministic conversion between international GPA scales.
* **Conversion Strategies**:
  * `_bavarian`: Modified Bavarian Formula for German systems:
    $$N = 1 + 3 \times \frac{G_{max} - G_{student}}{G_{max} - G_{min}}$$
    Result clamped to $[1.0, 4.0]$ where $1.0$ is best.
  * `_indian_to_us`: Non-linear breakpoint mapping from Indian 10-point scale to US 4.0 scale ($\ge 9.0 \to 4.0, \ge 8.5 \to 3.9, \dots$).
  * `_percentage_to_us`: Non-linear breakpoint table for percentage scales.
  * `_passthrough`: Identity mapping when scale matches target.
* **Potential Problems & Edge Cases**:
  * *Division by Zero*: If $G_{max} \le G_{min}$ in Bavarian calculation, raises `ValueError`.
  * *CGPA Out of Bounds*: Validates $0.0 \le cgpa \le scale$. Raises `ValueError` if student GPA is negative or exceeds scale.

#### `src/scholarship_finder/domain/url.py`
* **Purpose**: URL canonicalization, tracking removal, and Server-Side Request Forgery (SSRF) defense.
* **Key Functions**:
  * `canonicalize_url(url)`: Enforces `https`, strips `www.`, removes tracking params (`utm_*`, `fbclid`, `gclid`, etc.), sorts query params, strips fragments and trailing slashes.
  * `extract_domain(url)`: Returns clean hostname without port or www.
  * `is_private_or_loopback(url)`: SSRF firewall checking IPv4/IPv6 private networks (`127.0.0.0/8`, `10.0.0.0/8`, `192.168.0.0/16`, `169.254.0.0/16`, `::1/128`, `fc00::/7`, `fe80::/10`) and hostname aliases (`localhost`, `0.0.0.0`).
* **Potential Problems & Edge Cases**:
  * *DNS Rebinding*: `is_private_or_loopback` checks host string literally. A public hostname resolving to `127.0.0.1` must be blocked at socket connect time in the fetch adapter.

#### `src/scholarship_finder/domain/trust.py`
* **Purpose**: Deterministic domain trust tier classification.
* **Rules & Priority**:
  1. `AGGREGATOR`: Checks `_AGGREGATOR_DOMAINS` set (e.g. `scholarships.com`, `findamasters.com`). Aggregators can NEVER be Top-5 eligible directly.
  2. `RECOGNIZED_FUNDER`: Checks `_FUNDER_DOMAINS` set (e.g. `daad.de`, `fulbright.org`, `chevening.org`, `gatesfoundation.org`).
  3. `OFFICIAL_INSTITUTION`: Regex matches official academic TLDs (`.edu`, `.edu.*`, `.ac.*`, `uni-*.de`, `tu-*.de`, `.gov`, `.bund.de`, etc.).
  4. `UNKNOWN`: Unrecognized domains.
* **Potential Problems & Edge Cases**:
  * *Subdomains on Shared Platforms*: E.g., `github.io` or `wordpress.com` are not handled as institutions, classified as `UNKNOWN`.

#### `src/scholarship_finder/domain/span_verifier.py`
* **Purpose**: Stage-1 Anti-Hallucination deterministic engine.
* **Mechanism**:
  * Normalization pipeline applied symmetrically to BOTH page text and LLM evidence span:
    1. Unicode NFKC normalization
    2. Full casefold (`str.casefold()`)
    3. Curly quote unification (`‘’, “”` to ASCII `'` and `"`)
    4. Em/en dash unification to ASCII hyphen `-`
    5. Whitespace collapse (`\s+` to single space)
  * Verifies `norm_span in norm_page`. Computes character offset and context window ($\pm 150$ characters).
* **Potential Problems & Edge Cases**:
  * *HTML Entities*: If fetch adapter does not decode `&amp;` or `&nbsp;`, span matching fails even when text matches visually.

#### `src/scholarship_finder/domain/claim_verifier.py`
* **Purpose**: Stage-2 Anti-Hallucination verification of generated SOP sentences.
* **The Three Deterministic Guards**:
  1. **Referential Integrity Guard**: Every cited `FACT_XXX` in the sentence must exist in the `fact_pack`.
  2. **Numeric Token Guard**: Regex extracts all numeric tokens (integers, decimals, percentages, currency values). Every extracted number must be a substring of the cited facts.
  3. **Proper-Noun Allow-List Guard**: Regex extracts capitalized multi-word sequences. Unless present in `_ACADEMIC_STOP_SET` (e.g. "Computer Science", "Artificial Intelligence", "United States") or matching `[CANDIDATE_NAME]`, the proper noun must exist in the cited facts.
* **Potential Problems & Edge Cases**:
  * *Uncited Generic Numbers*: If an SOP says "I have 3 years of experience" but cites a fact stating "2021-2024", the number `3` is ungrounded and fails Guard 2 unless `3` is in the fact ledger.

#### `src/scholarship_finder/domain/tri_state.py`
* **Purpose**: Evaluates candidate qualifications against program requirements without LLM intervention.
* **Tri-State Semantics**:
  * `MET`: Qualification conclusively meets or exceeds requirement.
  * `UNMET`: Qualification conclusively falls below requirement. If `blocking=True`, composite score is hard-capped.
  * `UNKNOWN`: Requirement or candidate data is missing. Missing data is never treated as a silent failure; it is converted into an `ActionableGap`.
* **Potential Problems & Edge Cases**:
  * *String Degree Matching*: Uses alias dictionary (`masters` matches `msc`, `mtech`, `meng`). Unrecognized foreign degree titles default to `UNKNOWN`.

#### `src/scholarship_finder/domain/scoring.py`
* **Purpose**: Mathematical weighted scoring and Top-5 ranking engine.
* **Scoring Components**:
  * `academic_fit` (weight 0.25): Linear curve based on GPA margin.
  * `field_alignment` (weight 0.25): Float 0.0–1.0 from LLM alignment call.
  * `funding_strength` (weight 0.20): `FULL_TUITION_STIPEND` (100), `FULL_TUITION` (80), `PARTIAL` (50), `UNKNOWN` (40), `NONE` (20).
  * `feasibility` (weight 0.20): Language test and deadline feasibility.
  * `evidence_confidence` (weight 0.10): From `VerificationResult.completeness_score`.
* **Hard Rule Ceiling**: If `has_blocking_unmet == True`, total score is hard-capped at $25.0$.
* **Tiebreaking**: Highest composite $\to$ highest evidence confidence $\to$ lowest deadline distance.
* **Potential Problems & Edge Cases**:
  * *Duplicate URLs in Top 5*: `rank_opportunities()` enforces URL diversity by discarding secondary opportunities pointing to the same `official_url`.

#### `src/scholarship_finder/domain/pii.py`
* **Purpose**: PII Scrubber and in-memory `PIIVault`.
* **PII Patterns Scrubbed**:
  * Display name (case-insensitive whole-word regex).
  * Email addresses (`_EMAIL_RE`).
  * Aadhaar 12-digit numbers (`_AADHAAR_RE`).
  * Passport numbers (`_PASSPORT_RE`).
  * Social Security Numbers (`_SSN_RE`).
  * International Phone numbers (`_PHONE_RE`).
  * URLs containing the display name slug.
* **`PIIVault`**: In-memory map from token to raw string. Expiration tracked via `ttl_seconds` (default: 900s). Calling `purge()` or garbage collection clears internal dictionary.
* **Potential Problems & Edge Cases**:
  * *Over-aggressive Redaction*: Phone regex can match numerical ID sequences if not preceded by Aadhaar checks. Aadhaar regex runs first to prevent partial phone masking.
  * *Expired Vault at Render Time*: If a user leaves their browser open $> 15$ minutes before viewing results, the vault expires and `[CANDIDATE_NAME]` cannot be rehydrated, remaining as a placeholder in the UI.

#### `src/scholarship_finder/domain/reducers.py`
* **Purpose**: State transition validators enforcing append-only and single-write invariants on `SwarmState`.
* **Reducer Rules**:
  * `reduce_run_meta()`: Phase transitions are strictly monotonic (`INTAKE` $\to$ `PHASE_A` $\to$ `PHASE_B` $\to$ `COMPLETED`). Backward transitions raise `ValueError`. Outcome can only transition once from `IN_PROGRESS`.
  * `reduce_profile()`: Single-write only; raises `ValueError` on double-write.
  * `reduce_search_plans()`: Append-only, maximum 2 rounds.
  * `reduce_candidates()`: Keyed set union by `canonical_url`, retaining the candidate with higher `TrustTier`.
  * `reduce_opportunities()`: Keyed upsert by ID and `official_url`. Fields marked `EVIDENCED` can NEVER be demoted to `UNVERIFIED` by subsequent scraping passes.
  * `reduce_assessments()`: Single-write replace.
  * `reduce_application()`: Staged replace; rejects packages containing unverified sentences.
* **Potential Problems & Edge Cases**:
  * *Concurrency Exceptions*: Reducers raise `ValueError` on invariant violations. In LangGraph, an uncaught reducer exception immediately crashes the background thread.

#### `src/scholarship_finder/domain/enrichment_tips.py`
* **Purpose**: Data-driven resume analyzer generating user-facing guidance to improve scholarship eligibility.
* **Analysis Categories**: Missing GPA, borderline language test scores, lack of technical skills vs soft skills, project description brevity, and missing target countries.

---

### 5.3 Ports Layer (`src/scholarship_finder/ports/`)

All ports are pure abstract `typing.Protocol` classes:
* **`ports/document.py` (`DocumentPort`)**: `extract(pdf_bytes, max_pages) -> DocumentResult`
* **`ports/fetch.py` (`FetchPort`)**: `fetch(url, policy) -> FetchedDocument`
* **`ports/llm.py` (`LLMPort`)**: `complete(spec) -> LLMResult`, `stream(spec) -> AsyncIterator[str]`
* **`ports/profile_source.py` (`ProfileSourcePort`)**: `fetch_signals(query) -> ProfileSignals`
* **`ports/search.py` (`SearchPort`)**: `search(query) -> SearchResult`

---

### 5.4 Adapters Layer (`src/scholarship_finder/adapters/`)

#### `src/scholarship_finder/adapters/document_pdf.py`
* **Purpose**: Implements `DocumentPort` for extracting text from PDF files.
* **Mechanics**:
  * Primary: PyMuPDF (`fitz`), offloaded to thread pool via `loop.run_in_executor`.
  * Fallback: If PyMuPDF throws an exception or is not installed, catches and falls back to `pypdf`.
  * Scanned Document Detection: If $> 50\%$ of pages have $< 100$ characters, sets `is_scanned = True`.
  * OCR Integration: If `is_scanned = True` and `ocr_adapter` is injected, invokes `VisionOCRAdapter` to render pages to images and extract text.
* **Potential Problems & Edge Cases**:
  * *Massive PDFs*: Enforces hard ceiling `_MAX_BYTES = 50MB`. Files larger than 50MB return an empty document to prevent out-of-memory crashes.

#### `src/scholarship_finder/adapters/vision_ocr.py`
* **Purpose**: OCR fallback using multimodal LLMs (Gemini Vision).
* **Mechanics**: Renders PDF pages to PNG (150 DPI) via PyMuPDF, base64 encodes them, and sends them to `LLMTier.VISION` with instruction prompt. Limits OCR to max 20 pages to protect token budget.
* **Potential Problems & Edge Cases**:
  * *Privacy Constraint*: Scanned images cannot be scrubbed by regex before sending to the vision model; PII scrubbing occurs on the extracted text *after* OCR.

#### `src/scholarship_finder/adapters/fetch_chain.py`
* **Purpose**: 4-Tier Chain-of-Responsibility web content fetcher.
* **Tiers**:
  * *Tier 0*: In-memory TTL cache (`get_fetch_cache()`).
  * *Tier 1*: Jina Reader API (`https://r.jina.ai/{url}`) with Bearer authentication. Returns clean markdown.
  * *Tier 2*: Direct HTTP GET via `httpx.AsyncClient` with custom user-agent. Parses main content using `trafilatura`. Checks `robots.txt`.
  * *Tier 3*: Headless Chromium browser via Playwright (only active if `DEPLOYMENT_PROFILE=FULL`).
* **Escalation Triggers**:
  * Jina Reader timeout/error $\to$ escalates to Direct HTTP.
  * Direct HTTP returns JS shell markers ("enable javascript", $< 200$ chars) $\to$ escalates to Playwright.
* **SSRF Protection**: Rejects private/loopback IP addresses via `is_private_or_loopback(url)` before opening any socket.
* **Potential Problems & Edge Cases**:
  * *Anti-Bot Blocks*: If a site returns HTTP 403 or Cloudflare captcha markers, raises `BlockedError`, which trips the per-domain circuit breaker to prevent repeated futile requests to that domain.

#### `src/scholarship_finder/adapters/llm_gemini.py`
* **Purpose**: Primary LLM adapter utilizing the official `google-genai` SDK.
* **Mechanics**:
  * Routes requests by tier (`FAST` $\to$ Flash Lite, `REASONING` $\to$ Flash with `thinking_config`, `VISION` $\to$ Flash).
  * Error Translation: Maps `RESOURCE_EXHAUSTED` (429) to `RateLimitedError` or `QuotaExhaustedError`, `INVALID_ARGUMENT` to `ContextTooLongError`, and `PERMISSION_DENIED` to `AuthError`.
  * Thinking Token Configuration: Sets `thinking_budget` (1024 to 8192 tokens) for reasoning models.
* **Potential Problems & Edge Cases**:
  * *Empty Response on Safety Trigger*: If Gemini triggers a content safety block, `response.text` is empty; the adapter detects `finish_reason != STOP` and raises `TransientError` to allow failover to Groq.

#### `src/scholarship_finder/adapters/llm_openai.py`
* **Purpose**: Secondary/Failover LLM adapter supporting OpenAI-compatible APIs (Groq, OpenRouter, local Ollama).
* **Mechanics**:
  * Utilizes `AsyncOpenAI` client with lazy initialization.
  * Reasoning Tag Stripping: Regex strips inline `<think>.*?</think>` tags emitted by models like DeepSeek-R1 or Qwen-2.5 before JSON parsing.
* **Potential Problems & Edge Cases**:
  * *JSON Schema Strictness*: Some OpenAI-compatible providers do not support `response_format={"type": "json_schema"}`. The adapter falls back to prompt-guided JSON parsing with `json_repair`.

#### `src/scholarship_finder/adapters/profile_github.py`
* **Purpose**: Implements `ProfileSourcePort` via GitHub REST API v3.
* **Mechanics**: Fetches up to 15 public repositories, aggregating starred counts, programming language frequencies, and topic tags. Cached in `_github_cache` (24h TTL).
* **Potential Problems & Edge Cases**:
  * *Unauthenticated Rate Limits*: Shared IP addresses are capped at 60 requests/hour by GitHub. Providing `GITHUB_TOKEN` increases this to 5,000 requests/hour. All failures are non-blocking.

#### `src/scholarship_finder/adapters/search_tavily.py`
* **Purpose**: Primary search adapter using `tavily-python`.
* **Mechanics**: Executes basic (1 credit) or advanced (2 credits) searches. Injects domain inclusions/exclusions and time ranges.
* **Potential Problems & Edge Cases**:
  * *Monthly Credit Depletion*: Raises `UsageLimitExceededError` mapped to `QuotaExhaustedError`, halting further search attempts.

#### `src/scholarship_finder/adapters/search_jina.py`
* **Purpose**: Secondary search fallback using `s.jina.ai` or Serper.dev.

---

### 5.5 Resilience Stack (`src/scholarship_finder/resilience/`)

Every external API call must pass through the 6-layer middleware stack composed via `decorate()`:

```
Request ──> [Layer 1: Telemetry]
              ──> [Layer 2: Cache Check] ──(Hit)──> Return Cached Result
                    ──> [Layer 3: QuotaGovernor]
                          ──> [Layer 4: CircuitBreaker]
                                ──> [Layer 5: Retry Wrapper]
                                      ──> [Layer 6: Timeout Wrapper]
                                            ──> Concrete Adapter Execution
```

#### `src/scholarship_finder/resilience/telemetry.py` (Layer 1 - Outermost)
* **Function**: `telemetry_wrap()`. Records wall-clock latency, input/output/reasoning tokens, provider name, and outcome status into mutable `UsageRecord` list.

#### `src/scholarship_finder/resilience/cache.py` (Layer 2)
* **Function**: `TTLCache` (in-process LRU with expiration) and `RequestCache` (per-run request cache).
* **Cache Instances**:
  * `_search_cache`: 4 hours TTL (query hash).
  * `_fetch_cache`: 4 hours TTL (canonical URL hash).
  * `_extract_cache`: 8 hours TTL (content SHA-256).
  * `_github_cache`: 24 hours TTL.
* **Bypass**: Setting `force_refresh=True` bypasses cache reads while maintaining write-through.

#### `src/scholarship_finder/resilience/quota.py` (Layer 3)
* **Function**: `QuotaGovernor`. Manages token buckets for Requests Per Minute (RPM) smoothing and tracks Requests Per Day (RPD) ceilings.
* **Hard Reasoning Cap (DEC-1)**: Enforces $\le 4$ reasoning-tier calls per run. The 5th reasoning call immediately raises `QuotaExhaustedError`.
* **Judge Reserve**: Holds back 30% (`reserve_ratio=0.30`) of daily quota for evaluation/judge testing.

#### `src/scholarship_finder/resilience/breaker.py` (Layer 4)
* **Function**: `CircuitBreaker`. 4-State Machine:
  * `CLOSED`: Normal operation.
  * `OPEN`: Consecutive transient failures $\ge 3$. Fast-fails calls immediately. Transitions to `HALF_OPEN` after 30s cooldown.
  * `HALF_OPEN`: Allows 1 probe call. Success $\to$ `CLOSED`; Failure $\to$ `OPEN`.
  * `KNOWN_EXHAUSTED`: Tripped by `QuotaExhaustedError` (daily limit reached) or `ModelUnavailableError` (404/retired). Does not reset until midnight UTC.
* **Per-Domain Breakers**: `BreakerRegistry.get_domain(domain)` fast-fails web scraping on domains that return anti-bot captchas or 403s.

#### `src/scholarship_finder/resilience/retry.py` (Layer 5)
* **Function**: `with_retry()`. Implements jittered exponential backoff ($\pm 10\%$ jitter).
* **Retry Matrix**:
  * `TransientError`: 2 retries (backoff 0.5s, 2.0s).
  * `RateLimitedError`: Waits provider `retry_after` hint (if $\le 8$s), max 2 retries.
  * `NotFoundError`: Max 1 retry (handles DNS flaps).
  * `SchemaViolationError`: Max 1 retry for LLM output repair.
  * `QuotaExhaustedError`, `ModelUnavailableError`, `AuthError`: **0 retries** (immediate failover).

#### `src/scholarship_finder/resilience/timeout.py` (Layer 6 - Innermost)
* **Function**: `with_timeout()`. Wraps async call in `asyncio.wait_for()`, converting `asyncio.TimeoutError` into normalized `SFTimeoutError`.

#### `src/scholarship_finder/resilience/middleware.py`
* **Function**: `decorate(fn, cfg)`. Single entry point assembling all 6 layers.

---

### 5.6 Agents Layer (`src/scholarship_finder/agents/`)

#### `src/scholarship_finder/agents/intake.py`
* **Role**: Deterministic pre-processing before LangGraph execution.
* **Inputs**: Raw resume bytes, filename, MIME type, optional display name.
* **Outputs**: `IntakeResult` (scrubbed text, extracted GitHub username, page count, warnings) and initialized `PIIVault`.
* **What problems can it create?**
  * *Over-Scrubbing*: If candidate name matches common dictionary words (e.g. "Will", "Chase", "Hope"), whole-word regex may redact valid resume text into `[CANDIDATE_NAME]`.
  * *Unscrubbed PII*: Obscure international phone formats or non-standard name placements can bypass regex, leaking PII to downstream LLM prompts.

#### `src/scholarship_finder/agents/profile_enricher.py` (Agent 1)
* **Role**: Parses resume into `CandidateProfile`, enriches with GitHub, normalizes GPA.
* **Inputs**: Scrubbed text, GitHub handle.
* **Outputs**: Updated `SwarmState.profile`.
* **What problems can it create?**
  * *Malformed JSON Output*: LLM output truncation can produce incomplete JSON. Handled by `json_repair`, but unrecoverable syntax yields an empty profile, causing subsequent agents to fail.
  * *Hallucinated Education*: If candidate lists certificates, LLM might classify them as Master's degrees, skewing eligibility.

#### `src/scholarship_finder/agents/opportunity_scout.py` (Agent 2)
* **Role**: Generates search queries and discovers candidate URLs.
* **Inputs**: `CandidateProfile`.
* **Outputs**: Updated `SwarmState.candidates` and `SwarmState.search_plans`.
* **What problems can it create?**
  * *Over-Constrained Queries*: If LLM includes too many specific keywords, Tavily returns 0 results, forcing retries and degrading run quality.
  * *Aggregator Pollution*: If aggregators bypass domain exclusions, downstream auditor wastes fetch budget on listing directories rather than real applications.

#### `src/scholarship_finder/agents/source_auditor.py` (Agent 3)
* **Role**: Fetches web pages, extracts scholarship terms, verifies evidence spans.
* **Inputs**: List of `Candidate` objects.
* **Outputs**: List of verified `Opportunity` objects.
* **What problems can it create?**
  * *Slow Fetching & Timeouts*: Fetching 20 web pages sequentially causes Phase A deadline expiration ($> 75$s). Mitigated by wave fetching (10 URLs in parallel).
  * *Anti-Hallucination False Demotions*: If website text changes dynamically or contains HTML entities that differ slightly from LLM extraction, `verify_span()` fails, marking legitimate funding as `UNVERIFIED`.

#### `src/scholarship_finder/agents/ranking_strategist.py` (Agent 4)
* **Role**: Evaluates eligibility rules, calls reasoning LLM for field alignment, scores and ranks Top 5.
* **Inputs**: `CandidateProfile` and audited `Opportunity` objects.
* **Outputs**: `assessments` and `top_5` ID list.
* **What problems can it create?**
  * *Reasoning Budget Violation*: If alignment call is split into per-opportunity calls instead of 1 batched call, `QuotaGovernor` raises `QuotaExhaustedError` on the 5th call. Must remain strictly 1 batched call.
  * *Overly Aggressive Capping*: If an eligibility rule is mistakenly marked `blocking=True`, a high-fit opportunity is unfairly capped at 25 points.

#### `src/scholarship_finder/agents/application_strategist.py` (Agent 5)
* **Role**: Generates grounded Statement of Purpose and checklist for top opportunity.
* **Inputs**: Top Opportunity, CandidateProfile, PIIVault.
* **Outputs**: Verified `ApplicationPackage`.
* **What problems can it create?**
  * *SOP Stripping Collapse*: If the LLM generates sentences using ungrounded numeric metrics (e.g. "improved speed by 25%"), Stage-2 claim verification rejects them. If repair fails, sentences are stripped, potentially leaving an empty or disjointed SOP.
  * *Token Leakage*: Citations like `[FACT_001]` leaking into final user text if regex cleanup fails.

---

### 5.7 Engine Layer (`src/scholarship_finder/engine/`)

#### `src/scholarship_finder/engine/event_bus.py`
* **Role**: Thread-safe monotonic event stream (`deque(maxlen=500)`).
* **Guarantees**: Monotonic `seq` assignment, thread-safe synchronization via `threading.Lock`, protected terminal slot (`RUN_COMPLETED`, `RUN_DEGRADED`) immune to ring eviction.
* **What problems can it create?**
  * *Slow Consumers*: If an SSE client lags, events are dropped once ring buffer exceeds 500 entries (though terminal events are preserved).

#### `src/scholarship_finder/engine/gateway.py`
* **Role**: Bridges FastAPI web requests to background OS thread runner.
* **Responsibilities**:
  * Manages active `RunHandle` records.
  * Enforces `MAX_CONCURRENT_RUNS`.
  * Controls cancelation (`cancel()`).
  * Runs LangGraph in dedicated thread with independent `asyncio` event loop.
* **What problems can it create?**
  * *Zombie Threads*: If an adapter blocks on synchronous un-timeoutable I/O, the daemon thread remains alive in background until process termination.

#### `src/scholarship_finder/engine/graph.py`
* **Role**: StateGraph wiring nodes and conditional edges.
* **Routing Predicates**:
  * `_r1_enough_candidates`: If candidates $< 10$ and retries $< 1 \to$ `retry_scout`; else $\to$ `source_auditor`.
  * `_r2_has_opportunities`: If verified opportunities $\ge 1 \to$ `ranking_strategist`; else $\to$ `degraded_end`.
  * `_r3_sop_needed`: If `top_5` exists $\to$ `application_strategist`; else $\to$ `complete_end`.
* **What problems can it create?**
  * *State Deserialization Errors*: LangGraph returns dictionaries during transitions; if models are not reconstructed properly via `SwarmState(**data)`, attribute lookups crash.

---

### 5.8 API & Web Presentation Layer (`src/scholarship_finder/api/`, `web/`)

#### `src/scholarship_finder/api/main.py` & `routes.py`
* **Endpoints**:
  * `POST /api/upload`: Upload resume, starts run asynchronously.
  * `GET /api/events/{run_id}`: SSE event stream.
  * `GET /api/snapshot/{run_id}`: Polling snapshot of `SwarmState`.
  * `POST /api/cancel/{run_id}`: Cancel active run.
  * `GET /api/profile/{run_id}`: Enriched profile with data-driven tips.
  * `GET /api/opportunities/{run_id}`: Top opportunities with match rationales.
  * `GET /api/sop/{run_id}`: Rehydrated SOP with user name restored.
  * `GET /json/version`: Suppresses DevTools probe 404s.

#### `src/scholarship_finder/web/static/js/app.js`
* **Role**: Frontend client handling file upload, listening to SSE stream, dynamically updating telemetry progress bars, rendering opportunity cards, and providing SOP expand/collapse views.

---

## 6. Cross-Component Failure Modes & Cascading Risk Matrix

This table maps root-cause failures to their propagation paths, symptoms, and built-in system defenses:

| Root Cause Failure | Affected Component | Cascading Impact Across Pipeline | System Defense / Graceful Degradation |
|--------------------|--------------------|----------------------------------|---------------------------------------|
| **Gemini Daily Quota Exceeded (429/RPD)** | `adapters/llm_gemini.py` | `QuotaGovernor` catches $\to$ trips breaker to `KNOWN_EXHAUSTED`. If no Groq key, pipeline cannot generate queries or extract data. | `composition._CompositeLLMPort` automatically fails over to Groq/OpenAI. Pipeline continues uninterrupted. |
| **Search Engine Rate-Limited / 0 Hits** | `adapters/search_tavily.py` | `opportunity_scout` produces $< 10$ candidates $\to$ triggers `R1` retry. If retry also fails, `source_auditor` receives 0 URLs. | `_fallback_queries()` triggers keyword fallback; `R2` catches 0 opps and routes to `degraded_end`, emitting `RUN_DEGRADED` instead of crashing. |
| **Academic Site Employs Cloudflare Anti-Bot** | `adapters/fetch_chain.py` | Direct HTTP returns 403. If Playwright is off (`LITE` profile), URL fetch fails. | `BlockedError` caught $\to$ trips per-domain circuit breaker for 60s. Other wave URLs continue unaffected. |
| **LLM Emits Malformed / Truncated JSON** | `profile_enricher` / `source_auditor` | Standard `json.loads` raises `JSONDecodeError`. | Multi-tier repair in `source_auditor`: `json_repair` library $\to$ bracket extraction $\to$ brace-counting truncation recovery. |
| **Anti-Hallucination Rejects All Sentences** | `agents/application_strategist.py` | LLM generated claims not in fact ledger. Sentences fail numeric/proper-noun guards. | Repair prompt executes once. If repair still fails, sentence is stripped. If all stripped, `_generate_fallback_sop()` creates a grounded template SOP. |
| **Phase A Exceeds 75 Seconds** | `engine/gateway.py` | Slow web fetching on unresponsive university portals. | `asyncio.wait_for` in `RunGateway._run_graph` enforces hard deadline, aborting hanging fetches and emitting `RUN_DEGRADED`. |
| **User Leaves Browser Idle $> 15$ Minutes** | `domain/pii.py` (`PIIVault`) | In-memory TTL expires; vault is auto-purged from memory. | Calling `vault.rehydrate()` returns text with `[CANDIDATE_NAME]` intact without crashing. |
| **PDF Contains Only Scanned Images** | `adapters/document_pdf.py` | Text extraction returns $< 50$ chars. Profile enricher receives empty string. | Detected via `is_scanned=True` $\to$ triggers `VisionOCRAdapter` (Gemini Vision) to render pages to images and OCR text. |

---

## 7. Directory & File Reference Manifest

```
c:\ScholarShip_Finder\
├── pyproject.toml                         # Project metadata, dependencies, CLI scripts
├── .env.example                           # Template for model chains and API keys
├── README.md                              # User-facing setup & quickstart guide
├── AGENT_WORKFLOW.md                      # High-level mermaid agent diagrams
├── TECHNICAL_NOTE.md                      # Engineering design highlights & trade-offs
├── WORKFLOW_DIAGRAM.md                    # Detailed sequence diagrams
├── CODEBASE_MAP.md                        # THIS FILE (Comprehensive architectural map)
│
├── src/scholarship_finder/
│   ├── __init__.py                        # Version string
│   ├── cli.py                             # CLI entry point (scholarship-run)
│   ├── config.py                          # Typed Settings loaded from .env
│   ├── composition.py                     # Dependency Injection root & adapter factories
│   ├── errors.py                          # Normalized application error taxonomy
│   ├── preflight.py                       # Pre-flight canary probe CLI script
│   │
│   ├── domain/                            # PURE DETERMINISTIC DOMAIN LOGIC
│   │   ├── __init__.py
│   │   ├── models.py                      # Single source of truth for all Pydantic models
│   │   ├── gpa.py                         # Bavarian, Indian, Percentage GPA conversion
│   │   ├── url.py                         # URL canonicalization & SSRF guards
│   │   ├── trust.py                       # Academic & funder domain trust classifier
│   │   ├── span_verifier.py               # Stage-1 Anti-hallucination span matcher
│   │   ├── claim_verifier.py              # Stage-2 Anti-hallucination fact guards
│   │   ├── tri_state.py                   # MET / UNMET / UNKNOWN eligibility rules
│   │   ├── scoring.py                     # Weighted composite scoring & Top-5 ranking
│   │   ├── pii.py                         # PII Scrubber & in-memory PIIVault
│   │   ├── reducers.py                    # Monotonic state reducers for SwarmState
│   │   └── enrichment_tips.py             # Data-driven resume guidance generator
│   │
│   ├── ports/                             # ABSTRACT PROTOCOLS (PORT INTERFACES)
│   │   ├── __init__.py
│   │   ├── document.py                    # DocumentPort protocol
│   │   ├── fetch.py                       # FetchPort protocol
│   │   ├── llm.py                         # LLMPort protocol & TaskSpec
│   │   ├── profile_source.py              # ProfileSourcePort protocol
│   │   └── search.py                      # SearchPort protocol
│   │
│   ├── adapters/                          # CONCRETE SERVICE ADAPTERS
│   │   ├── __init__.py
│   │   ├── document_pdf.py                # PyMuPDF + pypdf document adapter
│   │   ├── vision_ocr.py                  # Gemini Vision OCR fallback adapter
│   │   ├── fetch_chain.py                 # Tier 0-3 fetch chain (Jina, HTTP, Playwright)
│   │   ├── llm_gemini.py                  # google-genai SDK LLM adapter
│   │   ├── llm_openai.py                  # AsyncOpenAI Groq/OpenAI LLM adapter
│   │   ├── profile_github.py              # GitHub REST API v3 adapter
│   │   ├── search_tavily.py               # Tavily search adapter
│   │   └── search_jina.py                 # Jina Search / Serper.dev adapter
│   │
│   ├── resilience/                        # 6-LAYER RESILIENCE DECORATOR STACK
│   │   ├── __init__.py
│   │   ├── telemetry.py                   # Layer 1: Metrics & usage tracking
│   │   ├── cache.py                       # Layer 2: In-memory TTL & Request caches
│   │   ├── quota.py                       # Layer 3: TokenBucket RPM & RPD governor
│   │   ├── breaker.py                     # Layer 4: 4-State circuit breaker
│   │   ├── retry.py                       # Layer 5: Jittered exponential backoff
│   │   ├── timeout.py                     # Layer 6: Hard per-call deadline wrapper
│   │   └── middleware.py                  # decorate() orchestrator unifying all 6 layers
│   │
│   ├── agents/                            # AUTONOMOUS PIPELINE AGENTS
│   │   ├── __init__.py
│   │   ├── intake.py                      # Intake Service (PDF extract + PII scrub)
│   │   ├── profile_enricher.py            # Agent 1: Resume parsing & GitHub enrichment
│   │   ├── opportunity_scout.py           # Agent 2: Search planning & discovery
│   │   ├── source_auditor.py              # Agent 3: Wave fetch & span verification
│   │   ├── ranking_strategist.py          # Agent 4: Eligibility, alignment & ranking
│   │   └── application_strategist.py      # Agent 5: Fact pack, SOP generation & claim check
│   │
│   ├── engine/                            # ORCHESTRATION & RUNTIME
│   │   ├── __init__.py
│   │   ├── event_bus.py                   # Bounded ring-buffer EventBus
│   │   ├── gateway.py                     # RunGateway background thread runner
│   │   └── graph.py                       # LangGraph StateGraph pipeline definition
│   │
│   ├── api/                               # FASTAPI HTTP / SSE PRESENTATION
│   │   ├── __init__.py
│   │   ├── main.py                        # FastAPI app initialization & lifespan
│   │   ├── routes.py                      # REST endpoints (/upload, /snapshot, etc.)
│   │   └── sse.py                         # Server-Sent Events response formatter
│   │
│   └── web/                               # USER INTERFACE ASSETS
│       ├── templates/index.html           # Main HTML single-page interface
│       └── static/
│           ├── css/style.css              # Custom responsive stylesheet
│           └── js/app.js                  # Frontend client state & SSE controller
│
└── tests/                                 # UNIT & INTEGRATION TEST SUITE
    ├── conftest.py                        # Pytest fixtures & mock adapters
    ├── test_domain/                       # Phase 1: Pure domain logic tests (177 tests)
    ├── test_resilience/                   # Phase 2: Breaker, quota, retry tests (50 tests)
    └── test_phase3/                       # Phase 3: End-to-end smoke tests (15 tests)
```

---

## 8. Agent Operations & Modification Playbook

When modifying or debugging this codebase, autonomous agents must adhere to the following decision protocols:

1. **Adding a New LLM Provider**:
   * Implement `ports/llm.py` (`LLMPort`).
   * Translate all provider exceptions into `errors.py` classes.
   * Add model names to `.env` chains—never hardcode strings in the adapter.
   * Wire the new adapter into `composition.py`.
2. **Modifying Eligibility Rules**:
   * Add the rule function strictly inside `domain/tri_state.py`.
   * Return `RuleResult` with `EligibilityResult.MET`, `UNMET`, or `UNKNOWN`.
   * Never return a boolean. If candidate or required data is missing, ALWAYS return `UNKNOWN`.
   * Register the rule inside `RankingStrategistAgent._evaluate_eligibility()`.
3. **Updating the State Schema**:
   * Add fields to `SwarmState` in `domain/models.py`.
   * Implement a corresponding merge function in `domain/reducers.py`.
   * Update `tests/test_domain/test_reducers.py` to verify idempotency and monotonic properties.
4. **Debugging Pipeline Timeouts**:
   * Check `RunDeadlines` in `domain/models.py` and `.env` (`PHASE_A_HARD_DEADLINE_S`).
   * Inspect telemetry records in `SwarmState.usage_records` to identify which adapter consumed excessive latency.
   * Verify if `_MAX_WAVES` or `_WAVE_SIZE` in `source_auditor.py` is causing network bottlenecks.
