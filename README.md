# 🎓 Autonomous Multi-Agent AI Scholarship Finder

> **Production-ready prototype** — Two-phase autonomous pipeline that discovers, audits, ranks, and drafts applications for graduate scholarships and programs.

---

## Quick Start

```bash
# 1. Clone and install
git clone <repo>
cd ScholarShip_Finder
pip install -e ".[dev]"

# 2. Configure API keys
cp .env.example .env
# Edit .env — add GEMINI_API_KEY + TAVILY_API_KEY at minimum

# 3. Pre-flight check (optional)
python -m scholarship_finder.preflight

# 4. Run the application
scholarship-run
# Open browser to http://localhost:8000
```

> **Zero-key demo**: leave keys blank and enable **REPLAY Mode** in the sidebar to see a pre-recorded run with the "Rahul" sample persona.

---

## How It Works

The system uses an autonomous multi-agent architecture where 5 specialized AI agents work together like a team. Here's the step-by-step process:

### Step 1: Intake & Privacy Protection
- **User uploads resume** (PDF, text, or markdown)
- **Document extraction:** PDF is parsed and text extracted
- **PII scrubbing:** Names, emails, phone numbers are removed automatically
- **Secure vault:** Sensitive data stored in memory with 15-minute auto-purge
- **No PII leaves intake:** All subsequent agents work with scrubbed text only

### Step 2: Phase A - Discovery & Verification (≤90 seconds)

#### Agent 1: Profile Enricher
- **Extracts:** Skills, projects, GPA, degree level, target fields, countries
- **Enriches:** Fetches GitHub profile to discover additional projects and achievements
- **Output:** Structured student profile with capabilities and preferences

#### Agent 2: Opportunity Scout
- **Plans:** Uses LLM to generate 5 optimized search queries (programs, scholarships, funders)
- **Searches:** Executes queries via Tavily API (primary) with Jina/Serper fallback
- **Collects:** Gathers up to 30 unique opportunity URLs
- **Deduplicates:** Removes duplicates by canonical URL
- **Output:** List of candidate opportunities with trust tier classification

#### Agent 3: Source Auditor
- **Fetches:** Visits official websites for each candidate
- **Extracts:** Requirements, deadlines, funding details, application process
- **Verifies:** Classifies trust tier (Official Institution, Recognized Funder, Aggregator)
- **Evidences:** Adds evidence badges for each verified requirement
- **Output:** Verified opportunities with full details and trust scores

#### Agent 4: Ranking Strategist
- **Scores:** Uses reasoning-tier LLM to score field alignment for all opportunities
- **Analyzes:** Identifies gaps (blocking vs non-blocking)
- **Ranks:** Selects top 5 based on composite score
- **Rationalizes:** Provides explanation for each match
- **Output:** Top-5 ranked opportunities with evidence, gaps, and rationale

### Step 3: Phase B - Application Strategy (≤120 seconds)

#### Agent 5: Application Strategist
- **Drafts:** Generates Statement of Purpose using reasoning-tier LLM
- **Cites:** Creates claim ledger with fact IDs [FACT_001, FACT_002, etc.]
- **Grounds:** Every claim in SOP is linked to verified facts from student profile
- **Verifies:** Anti-hallucination check ensures no invented facts
- **Output:** Application package with SOP, claim ledger, and evidence

### Step 4: Results & Privacy

- **Display:** Top-5 opportunities shown with match scores, funding, deadlines
- **Evidence:** Each requirement shows verification badge
- **Gaps:** Blocking and non-blocking gaps clearly identified
- **SOP:** Statement of Purpose with export options (Markdown/PDF)
- **Clear Data:** User can purge all data immediately; vault auto-purges after 15 minutes

### Key Innovations

1. **Autonomous Coordination:** Agents communicate via EventBus without human intervention
2. **Resilience Stack:** 6-layer middleware (telemetry, cache, quota governor, circuit breaker, retry, timeout)
3. **Anti-Hallucination:** Two-stage verification (span verification + claim ledger)
4. **Privacy by Design:** PII scrubbed before any external API call
5. **Hard Invariants:** ≤4 reasoning calls per run, phase deadlines, typed outcomes
6. **Real APIs:** Uses actual search (Tavily), LLM (Gemini/Groq), and GitHub APIs

### Architecture Details

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed diagrams showing:
- Agent swarm interactions
- Data flow between agents
- Resilience layer architecture
- Hexagonal ports and adapters
- Background thread execution model

---

## Architecture Overview

```
Upload Resume → Intake (PII Scrub) → [LangGraph Engine]
                                          │
                    ┌─────────────────────▼──────────────────────┐
                    │           PHASE A  (≤90s)                  │
                    │  Agent 1: Profile Enricher (LLM fast)      │
                    │  Agent 2: Opportunity Scout (LLM fast)     │
                    │  Agent 3: Source Auditor (fetch + verify)  │
                    │  Agent 4: Ranking Strategist (1× reasoning)│
                    │            → PHASE_A_READY (Top-5 emitted) │
                    └─────────────────────┬──────────────────────┘
                                          │
                    ┌─────────────────────▼──────────────────────┐
                    │           PHASE B  (≤120s)                 │
                    │  Agent 5: Application Strategist (SOP)     │
                    └────────────────────────────────────────────┘
```

### Key Components

| Layer | Files | Purpose |
|-------|-------|---------|
| **Domain** | `domain/` | Pure deterministic logic — GPA, scoring, PII, claim verification |
| **Ports** | `ports/` | Abstract interfaces (LLM, Search, Fetch, Document, ProfileSource) |
| **Adapters** | `adapters/` | Gemini, OpenAI, Tavily, Jina, GitHub, PDF, FetchChain |
| **Resilience** | `resilience/` | 4-state breaker, quota governor, retry, timeout, middleware |
| **Agents** | `agents/` | 5 agents + intake service |
| **Engine** | `engine/` | LangGraph graph, EventBus, RunGateway |
| **UI** | `ui/app.py` | Streamlit thin client |

---

## Hard Invariants

| Invariant | Enforcement |
|-----------|------------|
| **No hardcoded model IDs** | All tiers in `.env` chain strings |
| **≤4 reasoning calls/run** | `QuotaGovernor` raises at 5th call |
| **PII never leaves Intake** | `PIIScrubber` + `PIIVault` before any LLM/search call |
| **Anti-hallucination Stage 1** | `verify_span()` per extracted attribute |
| **Anti-hallucination Stage 2** | `verify_sop()` claim ledger per sentence |
| **Phase A ≤90s** | `RunGateway` `asyncio.wait_for` hard deadline |
| **Phase B ≤120s** | Combined deadline in gateway |
| **Typed terminal outcomes** | `COMPLETE / DEGRADED / PARTIAL / FAILED_SAFE` |
| **Discipline-agnostic prompts** | All prompts field-free; no domain mentions |
| **≥10 opportunities discovered** | Minimum candidates before skipping retry (hackathon requirement) |

---

## Environment Variables

```env
# Core (required for live mode)
GEMINI_API_KEY=...          # Google AI Studio key
TAVILY_API_KEY=...          # Tavily search

# Optional enrichment
OPENAI_API_KEY=...          # OpenAI-compatible fallback LLM
OPENAI_API_BASE_URL=...
JINA_API_KEY=...            # Jina secondary search / reader
SERPER_API_KEY=...          # Alternative secondary search
GITHUB_TOKEN=...            # GitHub profile enrichment

# LLM tier chains (model IDs never in code)
TIER_FAST_CHAIN=gemini-3.5-flash-lite,gemini-3.6-flash
TIER_REASONING_CHAIN=gemini-3.6-flash
TIER_VISION_CHAIN=gemini-3.5-flash

# Deployment
DEPLOYMENT_PROFILE=LITE     # LITE (Streamlit Cloud) | FULL (FastAPI)
MAX_CONCURRENT_RUNS=3
MAX_REASONING_CALLS_PER_RUN=4

# Demo Safety
DEMO_ACCESS_CODE=           # optional gate for public demos
JUDGE_RESERVE_RATIO=0.30    # hold 30% quota for judges

# Privacy
PII_VAULT_TTL_SECONDS=900   # 15-minute auto-purge

# PDF backend
PDF_BACKEND=pymupdf         # pymupdf (AGPL) | pypdf (MIT)
```

---

## Running Tests

```bash
# All tests (242 total)
python -m pytest tests/ -v

# By phase
python -m pytest tests/test_domain/          # Phase 1 — 177 tests
python -m pytest tests/test_resilience/     # Phase 2 — 50 tests
python -m pytest tests/test_phase3/         # Phase 3 — 15 tests

# Zero live API calls — all use mocks/fixtures
```

---

## Project Structure

```
ScholarShip_Finder/
├── src/scholarship_finder/
│   ├── domain/          # GPA, URL, trust, span verifier, tri-state, scoring, claim, PII, reducers
│   ├── ports/           # LLM, Search, Fetch, Document, ProfileSource protocols
│   ├── adapters/        # Gemini, OpenAI, Tavily, Jina, GitHub, PDF, FetchChain
│   ├── resilience/      # breaker, quota, retry, cache, timeout, middleware, telemetry
│   ├── agents/          # intake, profile_enricher, opportunity_scout, source_auditor,
│   │                    #   ranking_strategist, application_strategist
│   ├── engine/          # event_bus, gateway, graph (LangGraph)
│   ├── ui/              # Streamlit app + sample_persona/
│   ├── composition.py   # Dependency injection root
│   ├── config.py        # Pydantic-settings typed config
│   ├── errors.py        # Normalized error taxonomy
│   └── preflight.py     # Pre-flight canary probe
├── tests/
│   ├── test_domain/     # Unit tests for all domain services
│   ├── test_resilience/ # Breaker, quota, middleware tests
│   └── test_phase3/     # End-to-end smoke tests
├── streamlit_app.py     # `streamlit run streamlit_app.py`
├── pyproject.toml
├── .env.example
└── DECISIONS.md         # Architectural deviation log
```

---

## Architecture Decisions

See [`DECISIONS.md`](DECISIONS.md) for all deviations from the architecture report and their rationale.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for detailed system architecture diagrams.

See [`TECHNICAL_CHALLENGE.md`](TECHNICAL_CHALLENGE.md) for the most interesting experiment and biggest technical challenge.

See [`DEMO_RECORDING_GUIDE.md`](DEMO_RECORDING_GUIDE.md) for instructions on recording the demo video.

Key decisions:
- **DEC-IMPL-001**: Single `models.py` (avoids Pydantic v2 forward-ref issues)
- **DEC-IMPL-002**: `google-genai` SDK (recommended replacement for deprecated `google-generativeai`)
- **DEC-IMPL-006**: Single `decorate()` composition entry point for the 6-layer middleware
- **DEC-IMPL-008**: PDF adapter auto-falls back (PyMuPDF → pypdf) without raising

---

## Demo Instructions (Judge)

1. Set `DEMO_ACCESS_CODE` in `.env` (or leave blank to skip the gate)
2. Run `streamlit run streamlit_app.py`
3. Enable **REPLAY Mode** in the sidebar — uses the "Rahul Kumar / IIT Delhi" sample persona
4. Observe Phase A results (Top-5 cards with evidence badges) appear within ~5s in REPLAY mode
5. Enable **Force Live Refresh** to see real API calls (requires valid API keys)
6. Click **Clear My Data Now** to verify vault purge

---

## License

MIT (pypdf backend) / AGPL-3.0 (PyMuPDF — not distributed, local use only).
Select `PDF_BACKEND=pypdf` for fully MIT deployments.
