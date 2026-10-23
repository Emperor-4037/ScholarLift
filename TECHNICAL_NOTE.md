# Technical Note: AI Scholarship Finder

## Most Interesting Experiment: Anti-Hallucination with Claim-Ledger Verification

### The Problem
When AI systems generate application documents like Statements of Purpose (SOPs), they risk "hallucinating" facts - inventing achievements, misstating qualifications, or claiming opportunities that don't exist. This is particularly dangerous for academic applications where accuracy is critical.

### Our Solution: Two-Stage Verification Architecture

**Stage 1: Span Verification (Source Auditor Agent)**
- Every extracted attribute (funding, deadline, eligibility) must have a verbatim evidence span from the source page
- We verify that the extracted value actually appears in the normalized page text
- Mismatches demote the field to "UNVERIFIED" status
- This prevents the system from making claims about scholarships that aren't documented on official websites

**Stage 2: Claim-Ledger Verification (Application Strategist Agent)**
- Before generating the SOP, we build an "Allowed-Fact Pack" containing only verified information from the candidate's profile and the selected opportunity
- Each fact gets a unique ID (FACT_001, FACT_002, etc.)
- The LLM is instructed to use ONLY these facts when writing the SOP
- After generation, we run three deterministic guards on every sentence:
  1. **Referential Integrity**: Every cited fact ID must exist in the fact pack
  2. **Numeric Token Guard**: Every number in the sentence must appear in the cited facts
  3. **Proper-Noun Allow-List**: Capitalized multi-word terms must match facts or be in an academic stop list
- Sentences failing verification get one targeted repair attempt, then are stripped entirely

### Why This Matters
- **Trustworthiness**: Users can verify every claim by checking the fact ledger
- **Safety**: The system cannot invent achievements or qualifications
- **Debuggability**: Failures are deterministic and reproducible
- **Transparency**: The UI shows which facts were used for each claim

### Key Innovation
Unlike typical AI systems that use "soft" alignment (RLHF, constitutional AI), our approach uses **hard structural constraints**. The LLM cannot violate the constraints even if it wants to - the verification layer would catch it immediately.

---

## Biggest Technical Challenge: Parallel Processing with LangGraph Send API

### The Challenge
The Source Auditor Agent needs to fetch and verify 10-20 scholarship URLs. Doing this sequentially would be too slow (Phase A has a 75-second hard deadline). However, parallel fetching introduces concurrency challenges:
- How to coordinate multiple agents fetching simultaneously?
- How to aggregate results back into a single state?
- How to handle failures in individual fetches without crashing the entire pipeline?
- How to respect rate limits and API quotas?

### Our Solution: Send API with Mapper-Reducer Pattern

**Architecture:**
1. **Fan-out Node**: The Source Auditor creates a fan-out node that uses LangGraph's Send API to dispatch individual URLs to parallel mapper nodes
2. **Mapper Nodes**: Each mapper independently fetches and verifies one URL
3. **Reducer Node**: All mapper results converge to a reducer that aggregates verified opportunities
4. **Concurrency Control**: We limit to 6 concurrent mappers and process max 15 candidates per run

**Implementation Details:**
```python
# From engine/graph.py
def _fan_out_candidates(state: SwarmState) -> list[Send]:
    candidates_to_process = state.candidates[:15]
    return [
        Send(
            "candidate_mapper",
            MapperState(candidate=candidate, run_id=state.run_id)
        )
        for candidate in candidates_to_process
    ]
```

**Key Challenges Overcome:**

1. **State Consistency**: Each mapper needs access to shared resources (LLM, fetch adapter) but must not corrupt shared state. Solution: Dependency injection at graph construction time.

2. **Error Isolation**: If one mapper fails, it shouldn't crash the entire run. Solution: Each mapper returns empty results on failure, allowing the reducer to continue with successful fetches.

3. **Resource Limits**: Parallel fetching could exhaust API quotas. Solution: Built-in quota governor tracks usage across all mappers and enforces per-run limits.

4. **Progress Tracking**: Users need to see real-time progress during parallel operations. Solution: Each mapper publishes progress events to the EventBus, which the UI streams via SSE.

### Why This Matters
- **Performance**: Reduced Phase A execution time from ~120s to ~60s (2x speedup)
- **Scalability**: Can handle 20+ candidates within the hard deadline
- **Resilience**: Individual failures don't crash the pipeline
- **User Experience**: Real-time progress updates during parallel processing

### Key Innovation
We integrated LangGraph's Send API (typically used for map-reduce in data processing) into an agent coordination pattern. This is a novel application of the Send API - most LangGraph examples use it for simple parallel tasks, but we used it for complex multi-agent coordination with shared resources and error isolation.

---

## Additional Technical Highlights

### Hexagonal Architecture
The system uses hexagonal (ports and adapters) architecture:
- **Domain layer**: Pure business logic (GPA normalization, scoring, eligibility rules)
- **Ports**: Abstract interfaces (LLM, Search, Fetch, Document, ProfileSource)
- **Adapters**: Concrete implementations (Gemini, Tavily, GitHub, PDF, FetchChain)
- **Benefits**: Easy to swap implementations, testable domain logic, clear separation of concerns

### 6-Layer Resilience Stack
Every external API call is wrapped in a 6-layer middleware chain:
1. **Telemetry**: Records usage, latency, outcomes
2. **Cache**: Cross-run and per-run caching to reduce API calls
3. **Quota Governor**: Enforces per-run limits on expensive operations
4. **Circuit Breaker**: Fast-fails on repeated failures to prevent cascading errors
5. **Retry**: Jittered exponential backoff for transient errors
6. **Timeout**: Hard deadline enforcement per call

### Privacy by Design
- **PII Scrubbing**: Names, emails, phone numbers removed before any LLM/search call
- **PII Vault**: Sensitive data stored in-memory with 15-minute auto-purge
- **Local Rehydration**: Name placeholders replaced only when displaying results to the user
- **No Training Data**: We use may_train=False models to prevent data from being used for training

### Real-Time Progress Streaming
- **EventBus**: Ring-buffer event bus for agent communication
- **SSE**: Server-Sent Events stream real-time progress to the web UI
- **Telemetry Log**: Users can see exactly what each agent is doing
- **Phase Tracking**: Clear indication of Phase A (discovery) vs Phase B (application strategy)

---

## Performance Characteristics

- **Phase A (Discovery)**: ~60-75 seconds (hard deadline)
- **Phase B (Application Strategy)**: ~30-45 seconds
- **Total Runtime**: ~90-120 seconds for complete analysis
- **Candidates Processed**: 10-20 per run
- **Verified Opportunities**: 5-10 per run
- **LLM Calls**: ~15-20 per run (≤4 reasoning calls enforced)
- **Search Credits**: ~10-15 per run (Tavily API)

---

## Future Improvements

1. **Streaming SOP Generation**: Stream SOP chunks as they're generated (already have events, not used in UI)
2. **Multi-Document Upload**: Support transcripts, certificates alongside resume
3. **Advanced Caching**: Invalidation strategies for stale scholarship data
4. **Metrics Export**: Prometheus endpoints for monitoring
5. **Request Tracing**: Distributed tracing for debugging complex runs

---

## Conclusion

This system demonstrates that AI agents can work together autonomously while maintaining safety, trustworthiness, and performance. The anti-hallucination architecture ensures users can trust the results, while the parallel processing approach keeps the system fast and responsive. The hexagonal architecture and resilience stack make the system maintainable and production-ready.
