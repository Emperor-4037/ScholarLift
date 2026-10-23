# Technical Challenge Note

## Most Interesting Experiment: Background Thread Execution for Streamlit Integration

### The Problem

When integrating the autonomous multi-agent system with Streamlit, we encountered a critical issue: **the agents would freeze and never complete**. The system would show "Initializing" in the telemetry but never progress.

### Root Cause Analysis

Streamlit executes each page rerun in a script runner that manages its own event loop. When we tried to run the LangGraph agents using `asyncio.create_task()` within the Streamlit event loop, the background tasks would be starved of CPU cycles because:

1. Streamlit's script runner pauses or resets its event loop on each page rerun
2. Background asyncio.Task instances attached to the script loop remained frozen
3. The agents would start but never get scheduled to execute

### The Experiment

We tried multiple approaches:

#### Attempt 1: Direct asyncio.run() in Streamlit
```python
# Failed - Streamlit already has an event loop
result = asyncio.run(agent.execute())
```
**Result:** `RuntimeError: This event loop is already running`

#### Attempt 2: nest_asyncio Patch
```python
import nest_asyncio
nest_asyncio.apply()
# Run async code in Streamlit
```
**Result:** Allowed some async code to run, but background tasks still froze on page reruns

#### Attempt 3: asyncio.create_task() in Streamlit
```python
task = asyncio.create_task(agent.execute())
```
**Result:** Tasks created but never scheduled - agents stuck at "Initializing"

#### Attempt 4: Dedicated Background Thread (SUCCESS) ✅
```python
import threading
import asyncio

def run_in_background():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(agent.execute())

thread = threading.Thread(target=run_in_background, daemon=True)
thread.start()
```
**Result:** Agents execute reliably regardless of Streamlit reruns!

### The Solution

We implemented a dedicated OS thread per run that hosts its own fresh event loop:

```python
# From DEC-IMPL-016 in DECISIONS.md
def start_run(self, initial_state, graph_runner, bus, is_replay=False):
    """Launch run in a background daemon thread with isolated event loop."""
    run_id = str(uuid.uuid4())

    def _run_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                graph_runner(initial_state, bus, is_replay=is_replay)
            )
        finally:
            loop.close()

    thread = threading.Thread(target=_run_loop, daemon=True)
    thread.start()

    self.active_runs[run_id] = {
        "thread": thread,
        "bus": bus,
        "start_time": time.time(),
    }
    return run_id
```

### Key Insights

1. **Isolation is critical:** Each run needs its own event loop, not shared with Streamlit
2. **Threading works:** OS threads are not affected by Streamlit's event loop management
3. **Daemon threads:** Using daemon threads ensures cleanup on app shutdown
4. **Cross-thread communication:** EventBus must use `threading.Lock` instead of `asyncio.Lock`

### Trade-offs

**Pros:**
- Agents execute reliably and predictably
- No coupling to Streamlit's event loop behavior
- Progress continues even if user navigates away
- Cleaner separation of concerns

**Cons:**
- More complex threading code
- Need to manage thread lifecycle
- Cross-thread communication requires locks

### What We Learned

1. **Don't fight the framework:** Streamlit's event loop management is fundamental to its design
2. **Isolation over integration:** Better to isolate work than to tightly integrate with the framework's internals
3. **Thread safety matters:** When crossing thread boundaries, all shared state must be thread-safe
4. **Test under realistic conditions:** The issue only appeared during actual Streamlit usage, not in unit tests

---

## Biggest Technical Challenge: Managing LLM Quota and Reasoning Call Limits

### The Problem

The hackathon requirements specify using "real APIs" but also "at least 4 AI agents working together." With each agent potentially making multiple LLM calls, we faced:

1. **API rate limits:** Free-tier APIs have strict RPM (requests per minute) limits
2. **Cost concerns:** Reasoning-tier models are expensive
3. **Latency:** Too many sequential calls would exceed the 90-120 second phase deadlines
4. **Quota management:** Ensuring judges have reserved quota for live demos

### The Challenge

We needed to:
- Limit reasoning calls to ≤4 per run (hard invariant)
- Respect API rate limits
- Provide meaningful results within constraints
- Reserve quota for judges (30% reserve ratio)

### The Solution

We implemented a multi-layered quota governance system:

#### Layer 1: Quota Governor
```python
class QuotaGovernor:
    def __init__(self, profiles, reserve_ratio=0.30, max_reasoning_per_run=4):
        self.profiles = profiles  # Provider/model RPM/RPD limits
        self.reserve_ratio = reserve_ratio
        self.max_reasoning_per_run = max_reasoning_per_run
        self.run_reasoning_count = 0

    def check_quota(self, provider, model, tier):
        # Check provider limits
        if tier == LLMTier.REASONING:
            self.run_reasoning_count += 1
            if self.run_reasoning_count > self.max_reasoning_per_run:
                raise QuotaExceededError("Max 4 reasoning calls per run")
        # Check RPM/RPD limits...
```

#### Layer 2: Tiered Model Chains
```env
# Fast tier (bulk operations)
TIER_FAST_CHAIN=gemini-3.5-flash-lite,gemini-flash-lite-latest

# Reasoning tier (expensive, limited)
TIER_REASONING_CHAIN=gemini-3.5-flash

# Vision tier (document analysis)
TIER_VISION_CHAIN=gemini-3.5-flash-lite
```

#### Layer 3: Batched Reasoning Calls
Instead of one LLM call per opportunity, we batch all opportunities together:

```python
# From DEC-IMPL-010
# Agent 4 scores ALL opportunities in ONE batched prompt
prompt = f"""
Score these {len(opportunities)} opportunities together:
{opportunity_summary}

Return alignment scores for each.
"""
# Single reasoning call instead of 10-20 individual calls
```

#### Layer 4: Judge Reserve Ratio
```python
if is_judge:
    # Reserve 30% of quota for judges
    effective_quota = total_quota * (1 - reserve_ratio)
else:
    effective_quota = total_quota
```

### Results

- **≤4 reasoning calls per run:** Enforced by QuotaGovernor
- **Fast tier for bulk work:** Search, extraction, enrichment use fast models
- **Reasoning tier only when needed:** Ranking and SOP generation
- **Judge quota protection:** 30% reserved for live demos
- **Graceful degradation:** Falls back to stubs when quota exhausted

### What We Learned

1. **Batch aggressively:** Combining multiple items into one prompt saves quota and latency
2. **Tier your models:** Not every call needs the most expensive model
3. **Hard limits are good:** Enforcing invariants at runtime prevents runaway costs
4. **Reserve for judges:** Important for hackathon live demos

---

## Other Notable Technical Challenges

### 1. PII Scrubbing Before LLM Calls

**Challenge:** Student resumes contain sensitive information (names, emails, phone numbers) that should never be sent to LLM providers.

**Solution:** Implemented `PIIScrubber` that removes PII before any LLM or search call:
```python
class PIIScrubber:
    PII_PATTERNS = [
        r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',  # Email
        r'\b\d{10}\b',  # Phone (10 digits)
        r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b',  # Phone with formatting
    ]

    def scrub(self, text: str) -> str:
        for pattern in self.PII_PATTERNS:
            text = re.sub(pattern, '[REDACTED]', text)
        return text
```

**Insight:** PII must be removed at the intake layer, never in individual agents.

### 2. Anti-Hallucination with Claim Verification

**Challenge:** LLMs can invent facts. Students need trustworthy scholarship information.

**Solution:** Two-stage verification:
- **Stage 1:** `verify_span()` - Verify each extracted attribute against source
- **Stage 2:** `verify_sop()` - Build claim ledger for SOP, cite facts with IDs

```python
class ClaimLedger:
    entries: list[ClaimEntry]  # fact_id, value, source_url, confidence

def verify_sop(sop_text: str, claim_ledger: ClaimLedger) -> str:
    # Replace [FACT_001] with actual values from ledger
    # Ensures SOP is grounded in verified facts
```

**Insight:** Verification must be explicit, not implicit. Every claim needs a source.

### 3. Hexagonal Architecture Implementation

**Challenge:** Clean architecture in Python can lead to circular imports with Pydantic models.

**Solution:** From DEC-IMPL-001, we consolidated all domain models into a single `models.py` file:
```python
# domain/models.py - 30+ models in one file
class Opportunity(BaseModel): ...
class EvidenceRef(BaseModel): ...
class SwarmState(BaseModel): ...
```

**Insight:** Pragmatism over purity. Single file avoids circular imports while maintaining discoverability via section comments.

### 4. Real-Time Telemetry in Streamlit

**Challenge:** Show agent progress in real-time without blocking the UI.

**Solution:** Cursor-based event polling:
```python
def subscribe_events(self, run_id: str, cursor: int = 0):
    """Return events since cursor position."""
    return [e for e in self.events[run_id] if e.seq > cursor]

# In Streamlit (polls every 0.5s)
new_events = gateway.subscribe_events(run_id, st.session_state.event_cursor)
if new_events:
    st.session_state.events.extend(new_events)
    st.session_state.event_cursor = new_events[-1].seq
```

**Insight:** Polling is simpler than websockets for this use case. Cursor-based approach avoids re-sending old events.

---

## Conclusion

The most rewarding aspect of this project was transforming a theoretical multi-agent architecture into a working system that handles real-world constraints:

1. **Framework integration:** Solved Streamlit event loop freezing with background threads
2. **Resource management:** Implemented sophisticated quota governance
3. **Trust and verification:** Built anti-hallucination safeguards
4. **Privacy by design:** PII scrubbing before any external calls
5. **Resilience:** 6-layer middleware stack for production reliability

Each challenge taught us that **pragmatic solutions beat pure ideals**. The background thread solution isn't the "purest" async design, but it works reliably. The single models.py file isn't the "cleanest" separation, but it avoids circular imports. The quota governor isn't the most flexible system, but it enforces hard invariants.

This project demonstrates that building production-ready AI systems requires more than just calling APIs - it requires thinking about reliability, cost, privacy, and user experience from day one.
