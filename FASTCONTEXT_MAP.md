# FastContext Codebase Map - Scholarship Finder

> **Purpose**: Comprehensive codebase map preserved across all sessions for AI agents to understand the system architecture, data flow, and failure modes without re-exploration.

---

## 1. Architecture Overview

### 1.1 Component Hierarchy

```
FastAPI (api/main.py, api/routes.py)
    ↓
Intake Service (agents/intake.py)
    ↓ PDF extract + PII scrub → SwarmState + PIIVault
    ↓
RunGateway (engine/gateway.py)
    ↓ Background thread with isolated asyncio event loop
    ↓
LangGraph StateGraph (engine/graph.py)
    ↓ Orchestrates 5 agents:
    ├─ ProfileEnricher (agents/profile_enricher.py)
    ├─ OpportunityScout (agents/opportunity_scout.py)
    ├─ SourceAuditor (agents/source_auditor.py)
    ├─ RankingStrategist (agents/ranking_strategist.py)
    └─ ApplicationStrategist (agents/application_strategist.py)
    ↓
Resilience Stack (resilience/)
    ├─ Telemetry
    ├─ Cache
    ├─ QuotaGovernor
    ├─ CircuitBreaker
    ├─ Retry
    └─ Timeout
    ↓
Adapters (adapters/)
    ├─ LLM: llm_gemini.py, llm_openai.py
    ├─ Search: search_tavily.py, search_jina.py
    ├─ Fetch: fetch_chain.py
    ├─ Document: document_pdf.py, vision_ocr.py
    └─ Profile: profile_github.py
```

### 1.2 Threading Model

- **Main Thread**: FastAPI/Uvicorn event loop (port 8000)
- **Background Thread**: Each run spawns daemon thread with `asyncio.new_event_loop()`
- **Thread Safety**: EventBus uses `threading.Lock`, not `asyncio.Lock`
- **State Updates**: RunHandle uses `_state_lock` (threading.Lock) for state mutations
- **Race Condition Protection**: Only main thread reads snapshots, background thread writes state

### 1.3 Data Flow

1. **Upload**: POST /api/upload → file bytes → IntakeService.process()
2. **Intake**: PDF/Text extraction → PIIScrubber → PIIVault → scrubbed_text
3. **Gateway**: start_run() → creates RunHandle → spawns background thread
4. **Graph**: LangGraph executes nodes sequentially with LangGraph's max_concurrency=6
5. **Agents**: Each agent calls adapters via resilience stack
6. **State Updates**: Agents return new SwarmState → LangGraph merges → handle.set_state()
7. **API Polling**: GET /api/events/... → subscribe_events() → EventBus.since(cursor)
8. **Snapshot**: GET /api/profile/... → get_snapshot() → handle.get_state()

---

## 2. Component Map

### 2.1 API Layer

#### `api/main.py`
- **Purpose**: FastAPI application setup, CORS, static file serving
- **Key Functions**: 
  - `create_app()`: Main factory, mounts routers
- **Dependencies**: FastAPI, uvicorn

#### `api/routes.py`
- **Purpose**: HTTP endpoints for upload, event streaming, snapshots
- **Key Endpoints**:
  - POST /upload → start_run_with_intake_async()
  - GET /events/{run_id} → SSE streaming
  - GET /snapshot/{run_id} → full SwarmState
  - GET /profile/{run_id} → profile + enrichment tips
  - GET /opportunities/{run_id} → top 5 with assessments
  - GET /sop/{run_id} → rehydrated SOP
- **Critical Code**: Profile endpoint checks `snapshot.profile is None` → 404

#### `api/sse.py`
- **Purpose**: Server-Sent Events streaming implementation
- **Key Classes**: SSEStreamingResponse
- **Format**: `data: {json}\n\n` per message

### 2.2 Engine Layer

#### `engine/gateway.py`
- **Purpose**: Coordinates runs, manages background threads, state persistence
- **Key Classes**:
  - `RunGateway`: Singleton gateway managing all runs
  - `RunHandle`: Per-run state container with thread-safe state updates
  - `VaultRegistry`: Thread-safe PIIVault registry for SOP rehydration
- **Critical Methods**:
  - `start_run()`: Creates RunHandle, spawns daemon thread, returns run_id immediately
  - `get_snapshot()`: Returns current SwarmState (thread-safe via _state_lock)
  - `_run_in_thread()`: Creates isolated asyncio loop, runs graph
  - `_run_graph()`: Drives graph with deadline enforcement, updates handle state on success
- **Critical Issue**: `handle.set_state()` only called on graph success, not on failure/timeout
- **Fix Applied**: Added state preservation on TimeoutError, CancelledError, Exception to preserve partial state (especially profile)

#### `engine/graph.py`
- **Purpose**: LangGraph StateGraph orchestration of 5 agents
- **Key Functions**:
  - `make_graph()`: Builds StateGraph with nodes and conditional edges
  - `run_graph()`: Executes graph with config={"configurable": {"max_concurrency": 6}}
- **Nodes**:
  - `node_profile_enricher`: Phase A start, profile extraction
  - `node_opportunity_scout`: Search queries, candidate discovery
  - `node_retry_scout`: Retry with incremented counter
  - `node_source_auditor`: Wave fetch, extraction, verification
  - `node_ranking_strategist`: Scoring, top-5 selection, PHASE_A_READY event
  - `node_application_strategist`: SOP generation, claim verification
  - `node_degraded_end`: Terminal for insufficient opportunities
  - `node_complete_end`: Terminal success state
- **Routing Predicates**:
  - `_r1_enough_candidates()`: candidates >= 10 or retry_count >= max_retries
  - `_r2_has_opportunities()`: opportunities >= 1
  - `_r3_sop_needed()`: top_5 and profile exist
- **Fix Applied**: Added logging in node_profile_enricher to log profile extraction success/failure

#### `engine/event_bus.py`
- **Purpose**: Thread-safe ring buffer for event streaming
- **Key Classes**: EventBus (bounded circular buffer with cursor-based polling)
- **Thread Safety**: Uses `threading.Lock` for publish/subscribe operations
- **Methods**: `publish()`, `publish_sync()`, `since(cursor)`, `to_sse_message()`

### 2.3 Agents Layer

#### `agents/intake.py`
- **Purpose**: Pre-upload processing: PDF extraction, PII scrubbing
- **Key Classes**: IntakeService
- **Key Methods**:
  - `process()`: Extracts text, scrubs PII, returns IntakeResult + PIIVault
  - `_extract_name()`: Regex-based name extraction
  - `_extract_github_username()`: GitHub URL extraction
- **PII Patterns**: Email, phone, Aadhaar, SSN, passport, GitHub URLs
- **PII Tokens**: [CANDIDATE_NAME], [REDACTED_EMAIL], [REDACTED_PHONE], [REDACTED_ID]

#### `agents/profile_enricher.py`
- **Purpose**: Parse resume → CandidateProfile, normalize GPA, compute completeness
- **Key Classes**: ProfileEnricherAgent
- **Key Methods**:
  - `run()`: Orchestrates LLM extraction + GitHub enrichment (parallel)
  - `_extract_profile()`: LLM call with json_repair fallback
  - `_dict_to_profile()`: Maps LLM JSON → CandidateProfile with tolerance
  - `_normalize_gpas()`: Converts GPA to US 4.0, German Bavarian scales
  - `_compute_completeness()`: Sets completeness_flags
- **LLM Prompt**: _EXTRACT_PROFILE_PROMPT (escaped braces for .format())
- **Fix Applied**: Added logging for profile extraction success/failure and empty data

#### `agents/opportunity_scout.py`
- **Purpose**: Generate search queries, execute searches, canonicalize candidates
- **Key Classes**: OpportunityScoutAgent
- **Key Methods**:
  - `run()`: Plans queries, executes searches, deduplicates candidates
  - `_plan_queries()`: LLM query planning with retry on empty response
  - `_execute_searches()`: Parallel search execution with secondary fallback
  - `_fallback_queries()`: Hardcoded queries when LLM fails
- **Query Expansion**: Synonyms, exact phrases, academic terminology
- **Feature Toggle**: _ENABLE_QUERY_VARIANTS (disabled by default)
- **Fixes Applied**: 
  - Added missing asyncio import
  - Increased max_output_tokens to 8192
  - Added temperature parameter
  - Added retry mechanism on empty response
  - Fixed prompt formatting (escaped braces)

#### `agents/source_auditor.py`
- **Purpose**: Wave-pipelined fetch, batch extraction, Stage-1 anti-hallucination
- **Key Classes**: SourceAuditorAgent
- **Key Methods**:
  - `run()`: Orchestrates wave fetch (10 URLs/wave), batch extraction
  - `_extract_and_verify_batch()`: LLM extraction + span verification
  - `_verify_span()`: Exact substring matching in page text
  - `_is_listing_page()`: Detects scholarship aggregators
- **Stage-1 Anti-Hallucination**: verify_span() ensures evidence exists verbatim
- **Fix Applied**: Fixed None.lower() error by using (data.get("key") or "").lower()

#### `agents/ranking_strategist.py`
- **Purpose**: Tri-state eligibility scoring, field alignment, top-5 selection
- **Key Classes**: RankingStrategistAgent
- **Key Methods**:
  - `run()`: Scoring, eligibility evaluation, top-5 selection
  - `_evaluate_eligibility()`: Tri-state logic (MET/UNMET/UNKNOWN)
  - `_compute_composite_score()`: Weighted scoring (25% academic, 25% field, 20% funding, 20% feasibility, 10% evidence)
- **Hard Rule**: Any UNMET eligibility caps score at 25.0
- **LLM Tier**: REASONING (exactly 1 call per run per DEC-1)

#### `agents/application_strategist.py`
- **Purpose**: SOP generation with Stage-2 anti-hallucination (claim ledger)
- **Key Classes**: ApplicationStrategistAgent
- **Key Methods**:
  - `run()`: Builds fact pack, generates SOP, verifies claims, rehydrates name
  - `_build_fact_pack()`: Extracts verified facts from top-1 opportunity
  - `_verify_and_repair_sop()`: Referential integrity, numeric grounding, proper-noun guards
- **Stage-2 Verification**:
  - Guard 1: All cited fact IDs exist in fact pack
  - Guard 2: All numeric tokens appear in cited facts
  - Guard 3: Proper nouns match facts or academic stop-words
- **Repair Loop**: 1 targeted repair, then strip if still failing
- **Fix Applied**: Added null check for re_results[0]

### 2.4 Domain Layer

#### `domain/models.py`
- **Purpose**: Pydantic models for type-safe data contracts
- **Key Models**:
  - SwarmState: Central blackboard with all run data
  - CandidateProfile: Education, skills, projects, preferences
  - Opportunity: Scholarship/program with verification status
  - EligibilityAssessment: Tri-state eligibility + component scores
  - ApplicationPackage: SOP with verified sentences
  - RunMeta: Phase, outcome, budgets, deadlines
- **Critical Invariant**: Profile is single-write (raise if already set)

#### `domain/reducers.py`
- **Purpose**: Pure functions for state updates, enforces invariants
- **Key Functions**:
  - `reduce_profile()`: Single-write, raises if already set
  - `reduce_candidates()`: Keyed union by canonical_url, keep highest trust_tier
  - `reduce_opportunities()`: Keyed upsert, EVIDENCED fields never demoted
  - `reduce_assessments()`: Single-write for assessments + top_5
  - `reduce_application()": Staged replace, only accept fully-verified
- **Critical**: Monotonic phase transitions, single-write invariants

#### `domain/gpa.py`
- **Purpose**: Deterministic GPA normalization strategies
- **Key Functions**:
  - `normalize_gpa()`: Converts between scales (10.0, 100.0, 4.0, Bavarian)
  - Strategies: passthrough, indian_to_us, percentage_to_us, bavarian

#### `domain/pii.py`
- **Purpose**: PII redaction and vault for secure PII storage
- **Key Classes**: PIIVault, PIIScrubber
- **PII Types**: Name, email, phone, ID numbers, URLs
- **TTL**: Default 900 seconds (15 minutes)

#### `domain/span_verifier.py`
- **Purpose**: Stage-1 anti-hallucination: exact substring verification
- **Key Functions**: `verify_span()` - checks if evidence exists verbatim in text

#### `domain/claim_verifier.py`
- **Purpose**: Stage-2 anti-hallucination: claim-ledger verification
- **Key Functions**: `verify_sop()`, `verify_sentence()`
- **Guards**: Referential integrity, numeric grounding, proper-noun allow-list

#### `domain/enrichment_tips.py`
- **Purpose**: Data-driven enrichment tips based on profile gaps
- **Key Functions**: `generate_enrichment_tips()`, `format_tips_for_display()`
- **Fix Applied**: Fixed low_gpa_edu[0] by assigning to variable first

#### `domain/url.py`
- **Purpose**: URL normalization and SSRF protection
- **Key Functions**: `canonicalize_url()`, `extract_domain()`, `is_private_or_loopback()`
- **Fix Applied**: Added check for host.split(":")[0] returning empty list

### 2.5 Ports Layer

#### `ports/llm.py`
- **Purpose**: LLM capability abstraction (LLMPort protocol)
- **Key Classes**: TaskSpec, LLMResult, LLMUsage
- **Tiers**: FAST, REASONING, VISION

#### `ports/search.py`
- **Purpose**: Search capability abstraction (SearchPort protocol)
- **Key Classes**: SearchQuery, SearchResult, SearchResultItem

#### `ports/fetch.py`
- **Purpose**: Web fetch capability abstraction (FetchPort protocol)
- **Key Classes**: FetchedDocument, FetchPolicy

#### `ports/document.py`
- **Purpose**: Document extraction abstraction (DocumentPort protocol)
- **Key Classes**: DocumentResult, ExtractedPage

#### `ports/profile_source.py`
- **Purpose**: Profile signals abstraction (ProfileSourcePort protocol)
- **Key Classes**: ProfileSourceQuery, ProfileSignals

### 2.6 Adapters Layer

#### `adapters/llm_gemini.py`
- **Purpose**: Gemini API adapter (Google GenAI SDK)
- **Key Classes**: GeminiLLMAdapter
- **Features**: Vision support, JSON schema, streaming
- **Fix Applied**: Added asyncio import

#### `adapters/llm_openai.py`
- **Purpose**: OpenAI-compatible adapter (Groq, OpenRouter, etc.)
- **Key Classes**: OpenAICompatAdapter, OpenAILLMAdapter, _CompositeLLMPort
- **Features**: Event loop detection, client recreation on loop change
- **Fix Applied**: Added asyncio import, increased timeout to 120s

#### `adapters/search_tavily.py`
- **Purpose**: Tavily search API adapter
- **Key Classes**: TavilySearchAdapter
- **Fix Applied**: Added asyncio import

#### `adapters/search_jina.py`
- **Purpose**: Jina Search API adapter (secondary)
- **Key Classes**: JinaSearchAdapter, SerperAdapter

#### `adapters/fetch_chain.py`
- **Purpose**: Tiered fetch chain (Cache → Jina → Direct HTTP → Playwright)
- **Key Classes**: FetchChain, JinaReaderFetcher, DirectHttpFetcher
- **Tiers**: 0=Cache, 1=Jina, 2=Direct+Trafilatura, 3=Playwright (FULL only)
- **Escalation Triggers**: 403/429, captcha, JS shell, empty content

#### `adapters/document_pdf.py`
- **Purpose**: PDF extraction with PyMuPDF (primary) + pypdf (fallback)
- **Key Classes**: PDFDocumentAdapter
- **OCR Threshold**: < 50 chars/page triggers vision OCR
- **Fix Applied**: Added asyncio import

#### `adapters/vision_ocr.py`
- **Purpose**: Gemini Vision OCR for scanned PDFs
- **Key Classes**: VisionOCRAdapter
- **Fix Applied**: Added asyncio import, removed redundant inline import

#### `adapters/profile_github.py`
- **Purpose**: GitHub REST API adapter for profile signals
- **Key Classes**: GitHubProfileAdapter
- **Features**: Repos, languages, topics, star counts

### 2.7 Resilience Layer

#### `resilience/breaker.py`
- **Purpose**: Circuit breaker for rate limit/quota exhaustion
- **Key Classes**: BreakerRegistry, CircuitBreaker
- **States**: CLOSED → OPEN ↔ HALF_OPEN → KNOWN_EXHAUSTED

#### `resilience/quota.py`
- **Purpose**: Token-bucket rate limiting and budget enforcement
- **Key Classes**: QuotaGovernor, QuotaProfile
- **DEC-1 Enforcement**: Counters reasoning calls, enforces ≤4 per run

#### `resilience/retry.py`
- **Purpose**: Retry decorator with backoff strategies
- **Key Function**: `retry_wrap()`
- **Strategies**: Exponential backoff for transient errors

#### `resilience/cache.py`
- **Purpose**: TTL cache for expensive operations
- **Key Functions**: `get_fetch_cache()`, `get_github_cache()`

#### `resilience/telemetry.py`
- **Purpose**: Metrics collection (latency, tokens, cache hits)
- **Key Function**: `telemetry_wrap()`

#### `resilience/timeout.py`
- **Purpose**: Timeout enforcement
- **Key Function**: `with_timeout()`

#### `resilience/middleware.py`
- **Purpose**: Composes all 6 decorators in correct order
- **Order**: Telemetry → Cache → Quota → Breaker → Retry → Timeout

### 2.8 Configuration

#### `config.py`
- **Purpose**: Pydantic settings with .env parsing
- **Key Classes**: Settings
- **Key Methods**: `get_settings()`, `fast_models()`, `reasoning_models()`, `vision_models()`
- **Model Chains**: Comma-separated in .env (TIER_FAST_CHAIN, etc.)

#### `composition.py`
- **Purpose**: Dependency injection root - wires all components
- **Key Functions**:
  - `build_gateway()`: Factory for fully-wired RunGateway
  - `start_run_with_intake_async()`: Async entry point
  - `start_run_with_intake()`: Sync entry point
- **Adapter Builders**: _build_llm_port(), _build_search_ports(), etc.
- **Composite LLM**: Tries Groq first, falls back to Gemini on transient errors

---

## 3. Data Contract Map

### 3.1 SwarmState Structure

```python
SwarmState:
  run_meta: RunMeta
  profile: CandidateProfile | None
  candidates: list[Candidate]
  search_plans: list[SearchPlan]
  opportunities: list[Opportunity]
  assessments: list[EligibilityAssessment]
  top_5: list[str]  # opportunity IDs
  application_package: ApplicationPackage | None
  vault_reference: str | None  # PIIVault ID for SOP rehydration
  force_refresh: bool  # Cache bypass flag
```

### 3.2 Key Data Models

#### CandidateProfile
- education: list[Education]
- skills: list[str]
- projects: list[Project]
- language_tests: list[LanguageTest]
- preferences: Preferences
- completeness_flags: dict[str, bool]
- github_signals: ProfileSignals | None

#### Opportunity
- id: str
- name: str
- institution_or_funder: str
- country: str
- degree_level: str
- official_url: str
- fields: list[OpportunityField]  # with status (VERIFIED/UNVERIFIED)
- funding: FundingInfo
- deadlines: list[DeadlineInfo]
- verification: VerificationInfo

#### EligibilityAssessment
- opportunity_id: str
- composite_score: float (0-100)
- component_scores: list[ComponentScore]
- gaps: list[GapInfo]
- rationale: str
- rank: int

#### ApplicationPackage
- sop_text: str
- sop_sentences: list[SOPSentence]  # with verified flag
- checklist: list[str]

### 3.3 Reducer Invariants

1. **profile**: Single-write, raises if already set
2. **assessments**: Single-write, raises if already set
3. **top_5**: Single-write (paired with assessments)
4. **run_meta.phase**: Monotonic forward only (INTAKE → PHASE_A → PHASE_B → COMPLETED)
5. **run_meta.outcome**: Set once from IN_PROGRESS to terminal state
6. **budgets**: Counters only increase
7. **opportunities**: EVIDENCED fields never demoted to UNVERIFIED
8. **candidates**: Keep highest trust_tier on collision

---

## 4. Configuration Map

### 4.1 Environment Variables

#### Required
- `GEMINI_API_KEY`: Gemini API key (for vision tasks)
- `TAVILY_API_KEY`: Tavily search API key
- `GROQ_API_KEY` or `OPENAI_API_KEY`: Groq/OpenAI API key (primary LLM)

#### Optional
- `GITHUB_TOKEN`: GitHub token for profile enrichment
- `JINA_API_KEY`: Jina Reader/Search API key (secondary search/fetch)
- `SERPER_API_KEY`: Serper API key (alternative secondary search)

#### Deployment
- `DEPLOYMENT_PROFILE`: "LITE" (no browser) or "FULL" (with Playwright)
- `MAX_CONCURRENT_RUNS`: Default 3
- `ENABLE_OCR`: Default True
- `OCR_THRESHOLD_CHARS`: Default 50
- `OCR_MAX_PAGES`: Default 20

#### Timeouts
- `PHASE_A_SOFT_DEADLINE_S`: Default 75
- `PHASE_A_HARD_DEADLINE_S`: Default 90
- `PHASE_B_DEADLINE_S`: Default 45

#### Budgets
- `MAX_REASONING_CALLS_PER_RUN`: Default 4 (DEC-1 enforcement)
- `FAST_CALLS_LIMIT`: Default 100
- `SEARCH_CREDITS_LIMIT`: Default 30

### 4.2 Model Chains

#### Format
Comma-separated list of model IDs ordered by preference.

#### Chains
- `TIER_FAST_CHAIN`: Primary fast-tier models
- `TIER_REASONING_CHAIN`: Reasoning-tier models
- `TIER_VISION_CHAIN`: Vision-capable models

---

## 5. Runtime Invariants

### 5.1 System Invariants

| # | Invariant | Enforcement | Violation Consequence |
|---|-----------|-------------|----------------------|
| INV-1 | No Model IDs in Code | .env parsing only | Hard-coded vendor locks |
| INV-2 | ≤4 Reasoning Calls/Run | QuotaGovernor counter | QuotaExhaustedError |
| INV-3 | Zero PII Outside Intake | PIIScrubber before graph | Data leakage to LLM/Search |
| INV-4 | Stage-1 Anti-Hallucination | verify_span() exact matching | Fabricated attributes |
| INV-5 | Stage-2 Anti-Hallucination | claim_verifier 3 guards | Invented qualifications |
| INV-6 | Phase A Hard Deadline | asyncio.wait_for() timeout | Zombie processes |
| INV-7 | Typed Monotonic State Merges | reducers.py checks | Overwriting verified facts |
| INV-8 | Thread Isolation | daemon thread + isolated loop | Event loop deadlock |
| INV-9 | Vendor Exception Boundary | adapters → errors.py hierarchy | Unhandled exceptions |

### 5.2 Critical Failure Points

1. **Profile 404 Error**:
   - Cause: `handle.set_state()` not called on graph failure
   - Fix: Preserve partial state on timeout/cancel/error
   - Check: Added logging in get_snapshot() when profile is None

2. **Empty LLM Response**:
   - Cause: Token limits, stop sequences, content filters
   - Fix: Increased max_output_tokens (8192), added retry with higher temperature
   - Check: Token usage logging

3. **Prompt Formatting KeyError**:
   - Cause: Using `{}` in prompts without escaping for .format()
   - Fix: Escaped JSON schema braces with `{{` and `}}`
   - Check: All prompts format successfully

4. **None Attribute Errors**:
   - Cause: `.get()` returns None when key exists with None value
   - Fix: Use `(data.get("key") or "")` pattern
   - Check: Defensive null checks in critical paths

---

## 6. Common Failure Modes

### 6.1 LLM Failures

**Symptom**: Empty or malformed JSON response
**Check**: Token usage, finish_reason, timeout settings
**Mitigation**: json_repair library, retry with higher temperature, fallback stub

### 6.2 Search Failures

**Symptom**: 0 candidates, 0 opportunities
**Check**: Tavily API key, rate limits, query quality
**Mitigation**: Secondary search (Jina/Serper), fallback hardcoded queries

### 6.3 Fetch Failures

**Symptom**: 403/429, captcha, JS shell pages
**Check**: Domain blocking, robots.txt, escalation tiers
**Mitigation**: Tiered fetch chain, per-domain circuit breaker

### 6.4 Thread Issues

**Symptom**: Run not progressing, state not updating
**Check**: Background thread status, event loop deadlock
**Mitigation**: Isolated event loop per thread, daemon threads

### 6.5 State Inconsistency

**Symptom**: 404 on profile endpoint, missing data
**Check**: handle.set_state() calls, reducer invariants
**Mitigation**: Partial state preservation on failure, state validation logging

---

## 7. Debugging Checklist

When investigating issues:

1. **Check logs for**: "profile extraction", "LLM returned empty", "graph failed"
2. **Check snapshot**: Verify profile is not None before returning 404
3. **Check thread status**: Is background thread running? Did it crash?
4. **Check LLM response**: Was JSON valid? Was it empty?
5. **Check timing**: Was snapshot queried before graph completed?
6. **Check invariants**: Did reducer raise on single-write violation?
7. **Check thread isolation**: Are we mixing main thread and background thread loops?

---

## 8. Recent Fixes Applied

1. **Missing asyncio imports** (4 files): llm_openai.py, opportunity_scout.py, search_tavily.py, vision_ocr.py
2. **None.lower() errors**: Fixed in source_auditor.py, opportunity_scout.py, composition.py, url.py
3. **IndexError risks**: Fixed in application_strategist.py, app.js, enrichment_tips.py
4. **Empty LLM response**: Increased tokens, added temperature, added retry, enhanced prompt
5. **Prompt formatting KeyError**: Escaped braces in all agent prompts
6. **Profile 404 error**: Added state preservation on graph failure, added debug logging
7. **State updates**: Added logging in graph.py node_profile_enricher

---

*Generated: 2026-10-31*
*Version: 1.0*
