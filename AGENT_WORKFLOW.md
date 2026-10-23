# Agent Workflow Diagram

## System Architecture

```mermaid
graph TB
    subgraph "User Interface"
        UI[FastAPI Web UI]
        UPLOAD[Upload Resume]
        TELEMETRY[Real-time Telemetry]
        RESULTS[Results Display]
    end

    subgraph "Intake Service"
        INTAKE[Intake Service]
        SCRUB[PII Scrubber]
        VAULT[PII Vault]
        EXTRACT[Document Extractor]
    end

    subgraph "Phase A: Discovery & Verification"
        A1[Profile Enricher Agent]
        A2[Opportunity Scout Agent]
        A3[Source Auditor Agent]
        A4[Ranking Strategist Agent]
    end

    subgraph "Phase B: Application Strategy"
        A5[Application Strategist Agent]
    end

    subgraph "External Services"
        LLM[LLM APIs<br/>Gemini/Groq]
        SEARCH[Search APIs<br/>Tavily/Jina]
        GITHUB[GitHub API]
        WEB[Web Fetch<br/>Jina Reader/HTTP]
    end

    subgraph "Internal Services"
        BUS[EventBus]
        STATE[SwarmState]
        CACHE[Cache Layer]
        BREAKER[Circuit Breaker]
    end

    %% User Flow
    UPLOAD --> INTAKE
    UI -->|SSE Stream| TELEMETRY
    TELEMETRY -->|Show Progress| UI
    RESULTS -->|Display| UI

    %% Intake Flow
    INTAKE --> EXTRACT
    EXTRACT --> SCRUB
    SCRUB --> VAULT
    INTAKE -->|Scrubbed Text| A1
    INTAKE -->|Vault Reference| A5

    %% Phase A Flow
    A1 -->|Profile Data| STATE
    A1 -->|GitHub Enrichment| GITHUB
    A1 -->|LLM Call| LLM
    A1 -->|Profile Event| BUS

    A1 -->|Enriched Profile| A2
    A2 -->|Generate Queries| LLM
    A2 -->|Execute Searches| SEARCH
    A2 -->|Candidates| STATE
    A2 -->|Search Event| BUS

    A2 -->|Retry Logic| A2
    A2 -->|Candidates| A3

    A3 -->|Fan-out| MAPPER[Parallel Mappers]
    MAPPER -->|Fetch URLs| WEB
    MAPPER -->|Extract Data| LLM
    MAPPER -->|Verify Spans| A3
    A3 -->|Verified Opps| STATE
    A3 -->|Audit Event| BUS

    A3 -->|Opportunities| A4
    A4 -->|Eligibility Rules| STATE
    A4 -->|Field Alignment| LLM
    A4 -->|Composite Score| STATE
    A4 -->|Top-5 Rankings| STATE
    A4 -->|Ranking Event| BUS

    %% Phase B Flow
    A4 -->|Phase A Ready| A5
    A5 -->|Build Fact Pack| STATE
    A5 -->|Generate SOP| LLM
    A5 -->|Claim Verification| VERIFY[Claim Ledger]
    VERIFY -->|Repair/Strip| A5
    A5 -->|Final SOP| STATE
    A5 -->|SOP Event| BUS

    %% State & Event Flow
    STATE -->|Read| RESULTS
    BUS -->|SSE| TELEMETRY

    %% Resilience
    LLM -->|Quota/Retry| BREAKER
    SEARCH -->|Quota/Retry| BREAKER
    WEB -->|Quota/Retry| BREAKER
    BREAKER -->|Cache| CACHE

    %% Styling
    classDef ui fill:#e1f5ff,stroke:#01579b,stroke-width:2px
    classDef agent fill:#fff3e0,stroke:#e65100,stroke-width:2px
    classDef external fill:#f3e5f5,stroke:#4a148c,stroke-width:2px
    classDef internal fill:#e8f5e9,stroke:#1b5e20,stroke-width:2px
    classDef intake fill:#fce4ec,stroke:#880e4f,stroke-width:2px

    class UI,UPLOAD,TELEMETRY,RESULTS ui
    class A1,A2,A3,A4,A5 agent
    class LLM,SEARCH,GITHUB,WEB external
    class BUS,STATE,CACHE,BREAKER internal
    class INTAKE,SCRUB,VAULT,EXTRACT intake
```

## Agent Roles & Responsibilities

### 1. Profile Enricher Agent
**Purpose**: Parse resume and enrich with GitHub profile

**Inputs**:
- Scrubbed resume text (PII removed)
- Optional GitHub username

**Actions**:
- Extract education, skills, projects, language tests
- Normalize GPA to 4.0 scale
- Fetch GitHub signals (projects, languages, activity)
- Compute profile completeness flags

**Outputs**:
- Structured CandidateProfile
- GitHub signals (if available)
- Profile completeness flags

**External APIs**:
- LLM (fast tier) for profile extraction
- GitHub API for profile enrichment

---

### 2. Opportunity Scout Agent
**Purpose**: Discover scholarship and program opportunities

**Inputs**:
- Enriched candidate profile
- Retry count (for second round)

**Actions**:
- Generate 3-5 search queries based on profile
- Execute searches via Tavily API
- Canonicalize URLs and classify trust tiers
- Deduplicate candidates
- Retry with different queries if insufficient results

**Outputs**:
- List of Candidate objects (URLs)
- Search plan with queries used

**External APIs**:
- LLM (fast tier) for query planning
- Tavily API for web search
- Jina/Serper API (secondary fallback)

---

### 3. Source Auditor Agent
**Purpose**: Fetch and verify opportunity details

**Inputs**:
- List of Candidate URLs

**Actions**:
- Fetch official websites (tiered: Jina Reader → HTTP → Browser)
- Extract opportunity details (funding, deadlines, eligibility)
- Verify each extracted attribute against source text (span verification)
- Detect and skip listing/aggregator pages
- Follow links from listing pages to find actual opportunities

**Outputs**:
- Verified Opportunity objects with evidence spans
- Verification results (completeness scores)

**External APIs**:
- LLM (fast tier) for batched extraction
- Jina Reader API for clean markdown
- HTTP clients for direct fetching
- Playwright (optional) for JS-rendered pages

**Special Feature**: Parallel processing via Send API - up to 6 concurrent mappers

---

### 4. Ranking Strategist Agent
**Purpose**: Evaluate eligibility and rank opportunities

**Inputs**:
- Verified opportunities
- Candidate profile

**Actions**:
- Evaluate eligibility using deterministic tri-state rules
- Derive actionable gaps (missing information)
- Score field alignment via LLM (1 reasoning call)
- Compute composite scores (GPA, funding, feasibility, etc.)
- Rank top 5 opportunities

**Outputs**:
- Eligibility assessments with gaps
- Top-5 ranked opportunity IDs
- Rationale for each match

**External APIs**:
- LLM (reasoning tier) for field alignment scoring

---

### 5. Application Strategist Agent
**Purpose**: Generate Statement of Purpose

**Inputs**:
- Top-ranked opportunity
- Candidate profile
- PII Vault (for name rehydration)

**Actions**:
- Build allowed-fact pack from profile + opportunity
- Generate SOP using only allowed facts
- Verify each sentence against fact ledger
- Repair or strip ungrounded sentences
- Rehydrate [CANDIDATE_NAME] placeholder
- Generate execution checklist

**Outputs**:
- Verified SOP with claim ledger
- Execution checklist
- Application package

**External APIs**:
- LLM (fast tier) for SOP generation
- LLM (fast tier) for repair attempts

---

## Data Flow

### SwarmState (Shared Blackboard)
The central state object that all agents read and update:

```python
SwarmState:
  - run_meta: Run metadata (phase, deadlines, budgets)
  - profile: CandidateProfile (set by Agent 1)
  - candidates: List of Candidate URLs (set by Agent 2)
  - opportunities: List of verified Opportunities (set by Agent 3)
  - assessments: Eligibility assessments (set by Agent 4)
  - top_5: Ranked opportunity IDs (set by Agent 4)
  - application_package: SOP and checklist (set by Agent 5)
  - usage_records: API usage telemetry
  - errors: Error list
```

### EventBus (Agent Communication)
Ring-buffer event bus for real-time progress:

```python
Event:
  - run_id: Unique identifier
  - seq: Sequence number
  - timestamp: When event occurred
  - node: Which agent emitted it
  - severity: INFO/WARNING/ERROR
  - kind: NODE_STARTED/PROGRESS/PHASE_A_READY/etc.
  - payload: Additional data
```

---

## Routing Logic

### R1: Enough Candidates?
```python
if candidates >= 8 or retry_count >= max_retries:
    → Source Auditor
else:
    → Retry Scout (generate new queries)
```

### R2: Has Opportunities?
```python
if opportunities >= 1:
    → Ranking Strategist
else:
    → Degraded End (mark as PARTIAL)
```

### R3: SOP Needed?
```python
if top_5 and profile:
    → Application Strategist
else:
    → Complete End
```

---

## Phase Deadlines

### Phase A (Discovery & Verification)
- **Soft Deadline**: 60s - Stop new fetches
- **Hard Deadline**: 75s - Abort Phase A
- **Agents**: Profile Enricher, Opportunity Scout, Source Auditor, Ranking Strategist

### Phase B (Application Strategy)
- **Deadline**: 45s independent
- **Agent**: Application Strategist
- **Trigger**: PHASE_A_READY event

---

## Error Recovery

### Agent-Level Recovery
- **Retry**: Transient errors (network issues) get 2 retries with exponential backoff
- **Failover**: LLM quota errors trigger failover to next model in chain
- **Graceful Degradation**: If insufficient opportunities found, mark as DEGRADED but continue

### System-Level Recovery
- **Circuit Breaker**: Repeated failures on a domain/model open the circuit
- **Quota Governor**: Enforces per-run limits to prevent exhausting monthly quotas
- **Timeout Enforcement**: Every call has a hard timeout; aborts on deadline

---

## Anti-Hallucination Mechanisms

### Stage 1: Span Verification (Source Auditor)
- Every extracted attribute must have a verbatim evidence span
- Verification checks if the value appears in the source text
- Mismatches demote field to UNVERIFIED status

### Stage 2: Claim-Ledger Verification (Application Strategist)
- Allowed-fact pack restricts what LLM can use
- Three deterministic guards per sentence:
  1. Referential integrity (fact IDs must exist)
  2. Numeric token guard (numbers must be in facts)
  3. Proper-noun allow-list (capitalized terms must match)
- Failing sentences get one repair attempt, then are stripped

---

## Privacy Protections

### PII Scrubbing (Intake)
- Names, emails, phone numbers removed before any external call
- Replaced with placeholders like [CANDIDATE_NAME]

### PII Vault
- Sensitive data stored in-memory
- 15-minute auto-purge TTL
- Local rehydration only (never sent to LLM)

### Data Policy
- Uses may_train=False models to prevent training on user data
- No data persistence beyond session
- User can immediately purge all data

---

## Performance Characteristics

### Concurrency
- **Sequential**: Profile enrichment, search planning, ranking, SOP generation
- **Parallel**: Opportunity discovery (up to 6 concurrent searches)
- **Parallel Map-Reduce**: Source auditor (up to 6 concurrent mappers)

### API Usage (Typical Run)
- **LLM Calls**: 15-20 total (≤4 reasoning calls)
- **Search Credits**: 10-15 (Tavily API)
- **Fetch Operations**: 10-20 web pages
- **GitHub API**: 1-2 calls (if username provided)

### Timing
- **Phase A**: 60-75 seconds
- **Phase B**: 30-45 seconds
- **Total**: 90-120 seconds
