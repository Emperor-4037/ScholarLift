# Architecture Diagram

## System Overview

```mermaid
graph TB
    subgraph "User Interface"
        UI[FastAPI Web UI]
    end

    subgraph "Intake Layer"
        INTAKE[Intake Service]
        PDF[PDF Document Adapter]
        PII[PII Scrubber]
        VAULT[PII Vault]
    end

    subgraph "Phase A: Discovery & Verification (≤90s)"
        AGENT1[Agent 1: Profile Enricher]
        AGENT2[Agent 2: Opportunity Scout]
        AGENT3[Agent 3: Source Auditor]
        AGENT4[Agent 4: Ranking Strategist]
    end

    subgraph "Phase B: Application Strategy (≤120s)"
        AGENT5[Agent 5: Application Strategist]
    end

    subgraph "Resilience Layer"
        CB[Circuit Breaker]
        QG[Quota Governor]
        RT[Retry Logic]
        TM[Timeout]
        CACHE[Cache]
        TEL[Telemetry]
    end

    subgraph "External Services"
        GITHUB[GitHub API]
        SEARCH[Search APIs<br/>Tavily/Jina/Serper]
        LLM[LLM APIs<br/>Gemini/Groq/OpenAI]
        WEB[Web Fetch<br/>Trafilatura]
    end

    subgraph "Event Bus"
        BUS[Event Bus]
    end

    UI -->|Upload Resume| INTAKE
    INTAKE --> PDF
    PDF --> PII
    PII --> VAULT
    VAULT -->|Scrubbed Text| AGENT1

    AGENT1 -->|Enriched Profile| BUS
    BUS --> AGENT2

    AGENT2 -->|Search Queries| SEARCH
    SEARCH -->|Opportunities| AGENT2
    AGENT2 -->|Raw Opportunities| BUS
    BUS --> AGENT3

    AGENT3 -->|Fetch URLs| WEB
    WEB -->|Content| AGENT3
    AGENT3 -->|Verified Opportunities| BUS
    BUS --> AGENT4

    AGENT4 -->|LLM Scoring| LLM
    LLM -->|Scores| AGENT4
    AGENT4 -->|Top-5 Ranked| BUS
    BUS --> AGENT5

    AGENT5 -->|SOP Generation| LLM
    LLM -->|SOP Text| AGENT5
    AGENT5 -->|Application Package| BUS
    BUS --> UI

    AGENT1 -.->|Resilience Wrappers| CB
    AGENT2 -.->|Resilience Wrappers| CB
    AGENT3 -.->|Resilience Wrappers| CB
    AGENT4 -.->|Resilience Wrappers| CB
    AGENT5 -.->|Resilience Wrappers| CB

    CB --> QG
    QG --> RT
    RT --> TM
    TM --> CACHE
    CACHE --> TEL

    AGENT1 -->|GitHub Profile| GITHUB
    AGENT2 -->|Web Search| SEARCH
    AGENT3 -->|Web Fetch| WEB
    AGENT4 -->|Reasoning| LLM
    AGENT5 -->|SOP Draft| LLM

    style UI fill:#e1f5ff
    style INTAKE fill:#fff4e1
    style AGENT1 fill:#d4edda
    style AGENT2 fill:#d4edda
    style AGENT3 fill:#d4edda
    style AGENT4 fill:#d4edda
    style AGENT5 fill:#d1e7dd
    style BUS fill:#f8d7da
    style CB fill:#ffeaa7
```

## Agent Swarm Details

```mermaid
graph LR
    subgraph "Agent 1: Profile Enricher"
        A1_IN[Input: Scrubbed Resume]
        A1_PROC[Process: Extract Skills,<br/>Projects, GPA]
        A1_EXT[External: GitHub Profile]
        A1_OUT[Output: Enriched Profile]
    end

    subgraph "Agent 2: Opportunity Scout"
        A2_IN[Input: Enriched Profile]
        A2_PROC[Process: Search Queries<br/>Based on Profile]
        A2_EXT[External: Search APIs]
        A2_OUT[Output: Raw Opportunities<br/>(10+ candidates)]
    end

    subgraph "Agent 3: Source Auditor"
        A3_IN[Input: Raw Opportunities]
        A3_PROC[Process: Fetch Official URLs,<br/>Extract Requirements,<br/>Verify Trust Tier]
        A3_EXT[External: Web Fetch]
        A3_OUT[Output: Verified Opportunities<br/>with Evidence]
    end

    subgraph "Agent 4: Ranking Strategist"
        A4_IN[Input: Verified Opportunities]
        A4_PROC[Process: Score Alignment,<br/>Analyze Gaps,<br/>Rank by Fit]
        A4_EXT[External: LLM Reasoning]
        A4_OUT[Output: Top-5 Ranked<br/>with Rationale]
    end

    subgraph "Agent 5: Application Strategist"
        A5_IN[Input: Top-5 + Profile]
        A5_PROC[Process: Draft SOP,<br/>Claim Ledger,<br/>Citations]
        A5_EXT[External: LLM Generation]
        A5_OUT[Output: Application Package<br/>(SOP + Evidence)]
    end

    A1_OUT --> A2_IN
    A2_OUT --> A3_IN
    A3_OUT --> A4_IN
    A4_OUT --> A5_IN

    style A1_IN fill:#e3f2fd
    style A1_OUT fill:#c8e6c9
    style A2_IN fill:#e3f2fd
    style A2_OUT fill:#c8e6c9
    style A3_IN fill:#e3f2fd
    style A3_OUT fill:#c8e6c9
    style A4_IN fill:#e3f2fd
    style A4_OUT fill:#c8e6c9
    style A5_IN fill:#e3f2fd
    style A5_OUT fill:#c8e6c9
```

## Resilience Architecture

```mermaid
graph TB
    subgraph "Resilience Stack (6 Layers)"
        direction TB
        L1[Layer 1: Telemetry<br/>Log all calls]
        L2[Layer 2: Cache<br/>Avoid duplicate calls]
        L3[Layer 3: Quota Governor<br/>Enforce ≤4 reasoning calls]
        L4[Layer 4: Circuit Breaker<br/>Fail fast on errors]
        L5[Layer 5: Retry Logic<br/>Transient error recovery]
        L6[Layer 6: Timeout<br/>Prevent hanging]
    end

    subgraph "Agent Call"
        AGENT[Agent Function]
    end

    subgraph "External Service"
        SVC[External API]
    end

    AGENT --> L1
    L1 --> L2
    L2 --> L3
    L3 --> L4
    L4 --> L5
    L5 --> L6
    L6 --> SVC

    style L1 fill:#ffe0b2
    style L2 fill:#ffe0b2
    style L3 fill:#ffcc80
    style L4 fill:#ffb74d
    style L5 fill:#ffa726
    style L6 fill:#ff9800
```

## Hexagonal Architecture (Ports & Adapters)

```mermaid
graph TB
    subgraph "Domain Layer (Core)"
        DOMAIN[Domain Services<br/>- GPA Calculator<br/>- URL Validator<br/>- Trust Scorer<br/>- Span Verifier<br/>- Claim Verifier]
    end

    subgraph "Ports (Interfaces)"
        PORT_LLM[LLM Port]
        PORT_SEARCH[Search Port]
        PORT_FETCH[Fetch Port]
        PORT_DOC[Document Port]
        PORT_PROFILE[Profile Source Port]
    end

    subgraph "Adapters (Implementations)"
        ADAPT_GEMINI[Gemini Adapter]
        ADAPT_OPENAI[OpenAI/Groq Adapter]
        ADAPT_TAVILY[Tavily Search]
        ADAPT_JINA[Jina Search]
        ADAPT_FETCH[Fetch Chain]
        ADAPT_PDF[PDF Document]
        ADAPT_GITHUB[GitHub Profile]
    end

    subgraph "External World"
        EXT_GEMINI[Gemini API]
        EXT_OPENAI[OpenAI/Groq API]
        EXT_TAVILY[Tavily API]
        EXT_JINA[Jina API]
        EXT_WEB[HTTP/Web]
        EXT_GITHUB_DB[GitHub]
    end

    DOMAIN --> PORT_LLM
    DOMAIN --> PORT_SEARCH
    DOMAIN --> PORT_FETCH
    DOMAIN --> PORT_DOC
    DOMAIN --> PORT_PROFILE

    PORT_LLM --> ADAPT_GEMINI
    PORT_LLM --> ADAPT_OPENAI
    PORT_SEARCH --> ADAPT_TAVILY
    PORT_SEARCH --> ADAPT_JINA
    PORT_FETCH --> ADAPT_FETCH
    PORT_DOC --> ADAPT_PDF
    PORT_PROFILE --> ADAPT_GITHUB

    ADAPT_GEMINI --> EXT_GEMINI
    ADAPT_OPENAI --> EXT_OPENAI
    ADAPT_TAVILY --> EXT_TAVILY
    ADAPT_JINA --> EXT_JINA
    ADAPT_FETCH --> EXT_WEB
    ADAPT_PDF --> EXT_WEB
    ADAPT_GITHUB --> EXT_GITHUB_DB

    style DOMAIN fill:#d1c4e9
    style PORT_LLM fill:#c5cae9
    style PORT_SEARCH fill:#c5cae9
    style PORT_FETCH fill:#c5cae9
    style PORT_DOC fill:#c5cae9
    style PORT_PROFILE fill:#c5cae9
    style ADAPT_GEMINI fill:#b2dfdb
    style ADAPT_OPENAI fill:#b2dfdb
    style ADAPT_TAVILY fill:#b2dfdb
    style ADAPT_JINA fill:#b2dfdb
    style ADAPT_FETCH fill:#b2dfdb
    style ADAPT_PDF fill:#b2dfdb
    style ADAPT_GITHUB fill:#b2dfdb
```

## Data Flow

```mermaid
sequenceDiagram
    participant User
    participant UI as FastAPI Web UI
    participant Intake as Intake Service
    participant Vault as PII Vault
    participant Bus as Event Bus
    participant A1 as Agent 1: Profile Enricher
    participant A2 as Agent 2: Opportunity Scout
    participant A3 as Agent 3: Source Auditor
    participant A4 as Agent 4: Ranking Strategist
    participant A5 as Agent 5: Application Strategist

    User->>UI: Upload Resume
    UI->>Intake: Process Document
    Intake->>Intake: Extract Text (PDF)
    Intake->>Intake: Scrub PII
    Intake->>Vault: Store PII (15-min TTL)
    Intake->>Bus: Publish scrubbed_text
    Bus->>A1: Trigger Profile Enrichment

    A1->>A1: Extract Skills/Projects
    A1->>A1: Fetch GitHub Profile
    A1->>Bus: Publish enriched_profile
    Bus->>A2: Trigger Opportunity Scout

    A2->>A2: Generate Search Queries
    A2->>A2: Call Search APIs
    A2->>Bus: Publish raw_opportunities
    Bus->>A3: Trigger Source Auditor

    A3->>A3: Fetch Official URLs
    A3->>A3: Extract Requirements
    A3->>A3: Verify Trust Tier
    A3->>Bus: Publish verified_opportunities
    Bus->>A4: Trigger Ranking Strategist

    A4->>A4: Score Alignment (LLM)
    A4->>A4: Analyze Gaps
    A4->>A4: Rank by Fit
    A4->>Bus: Publish top_5_ranked
    Bus->>A5: Trigger Application Strategist

    A5->>A5: Draft SOP (LLM)
    A5->>A5: Build Claim Ledger
    A5->>Bus: Publish application_package
    Bus->>UI: Display Results

    UI->>User: Show Top-5 + SOP
    User->>UI: Clear Data
    UI->>Vault: Purge PII
```

## Hard Invariants

```mermaid
graph TB
    subgraph "System Invariants"
        INV1[No Hardcoded Model IDs<br/>All in .env chain strings]
        INV2[≤4 Reasoning Calls/Run<br/>QuotaGovernor enforces]
        INV3[PII Never Leaves Intake<br/>Scrubbed before LLM/Search]
        INV4[Anti-Hallucination Stage 1<br/>verify_span per attribute]
        INV5[Anti-Hallucination Stage 2<br/>verify_sop claim ledger]
        INV6[Phase A ≤90s<br/>Hard deadline]
        INV7[Phase B ≤120s<br/>Combined deadline]
        INV8[Typed Terminal Outcomes<br/>COMPLETE/DEGRADED/PARTIAL/FAILED_SAFE]
        INV9[Discipline-Agnostic Prompts<br/>No domain mentions]
    end

    style INV1 fill:#ffcdd2
    style INV2 fill:#ffcdd2
    style INV3 fill:#ffcdd2
    style INV4 fill:#ffcdd2
    style INV5 fill:#ffcdd2
    style INV6 fill:#ffcdd2
    style INV7 fill:#ffcdd2
    style INV8 fill:#ffcdd2
    style INV9 fill:#ffcdd2
```

## Background Thread Execution (DEC-IMPL-016)

```mermaid
graph TB
    subgraph "FastAPI Main Thread"
        UI[FastAPI Web UI]
        API[API Endpoints]
    end

    subgraph "Background Thread (per run)"
        BG_LOOP[New Event Loop]
        GRAPH[LangGraph Runner]
        AGENTS[Agent Execution]
    end

    subgraph "Shared Resources"
        BUS[EventBus<br/>threading.Lock]
        VAULT[PII Vault]
    end

    UI -->|User Upload| API
    API -->|start_run_with_intake| UI
    UI -->|Launch Thread| BG_LOOP
    BG_LOOP --> GRAPH
    GRAPH --> AGENTS
    AGENTS -->|Publish Events| BUS
    BUS -->|SSE Events| UI

    style UI fill:#e1f5ff
    style API fill:#fff4e1
    style BG_LOOP fill:#d4edda
    style GRAPH fill:#d4edda
    style AGENTS fill:#d4edda
    style BUS fill:#f8d7da
```
