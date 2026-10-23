"""All domain models for the Scholarship Finder.

Kept in one file to avoid Pydantic v2 forward-reference complications.
Sections:
  1. Enums
  2. Evidence & Profile models
  3. Search & Candidate models
  4. Opportunity models
  5. Eligibility & Assessment models
  6. Application Package models
  7. Run metadata & observability models
  8. SwarmState (the central blackboard)
"""
from __future__ import annotations

import enum
import operator
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. Enums
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RunOutcome(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    DEGRADED = "DEGRADED"   # ran but <10 verified
    PARTIAL = "PARTIAL"     # top5 empty or cancelled
    FAILED_SAFE = "FAILED_SAFE"  # unusable input only
    CANCELLED = "CANCELLED"


class RunPhase(str, enum.Enum):
    INTAKE = "INTAKE"
    PHASE_A = "PHASE_A"
    PHASE_B = "PHASE_B"
    COMPLETED = "COMPLETED"


class OpportunityKind(str, enum.Enum):
    PROGRAM = "PROGRAM"
    SCHOLARSHIP = "SCHOLARSHIP"
    PROGRAM_WITH_FUNDING = "PROGRAM_WITH_FUNDING"


class FieldStatus(str, enum.Enum):
    EVIDENCED = "EVIDENCED"
    UNVERIFIED = "UNVERIFIED"


class EligibilityResult(str, enum.Enum):
    MET = "MET"
    UNMET = "UNMET"
    UNKNOWN = "UNKNOWN"   # missing data; never a silent failure


class FundingLevel(str, enum.Enum):
    FULL_TUITION_STIPEND = "FULL_TUITION_STIPEND"
    FULL_TUITION = "FULL_TUITION"
    PARTIAL = "PARTIAL"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"


class TrustTier(str, enum.Enum):
    OFFICIAL_INSTITUTION = "OFFICIAL_INSTITUTION"  # .edu, .ac.*, .gov
    RECOGNIZED_FUNDER = "RECOGNIZED_FUNDER"        # daad.de, fulbright.org, etc.
    AGGREGATOR = "AGGREGATOR"                      # scholarship portals (leads only)
    UNKNOWN = "UNKNOWN"


class GapSeverity(str, enum.Enum):
    CRITICAL = "CRITICAL"   # blocking UNMET
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class EventKind(str, enum.Enum):
    RUN_STARTED = "run_started"
    NODE_STARTED = "node_started"
    PROGRESS = "progress"
    RETRY_TRIGGERED = "retry_triggered"
    NODE_COMPLETED = "node_completed"
    PHASE_A_READY = "phase_a_ready"
    SOP_CHUNK = "sop_chunk"
    RUN_COMPLETED = "run_completed"
    RUN_DEGRADED = "run_degraded"
    ERROR = "error"


class EventSeverity(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class LLMTier(str, enum.Enum):
    FAST = "fast"
    REASONING = "reasoning"
    VISION = "vision"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. Evidence & Profile models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EvidenceRef(BaseModel):
    """Points to where a claim was sourced (document page or API call)."""
    source: str          # "doc:page", "gh:repo", "api:endpoint", "web:url"
    location: str        # page number, repo slug, URL, etc.
    span: Optional[str] = None   # verbatim text (15–400 chars) for web evidence
    offset: Optional[int] = None  # character offset in normalized page text
    fetched_at: Optional[datetime] = None

    @field_validator("span")
    @classmethod
    def validate_span_length(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not (15 <= len(v) <= 400):
            raise ValueError(f"Evidence span length must be 15–400 chars, got {len(v)}")
        return v


class NormalizedGPA(BaseModel):
    """GPA converted to a target scale; always labelled as an estimate."""
    value: float
    target_scale: str           # e.g. "us_4point", "german_1to4", "percentage"
    method: str                 # e.g. "bavarian_formula", "indian_to_us"
    confidence: float = Field(ge=0.0, le=1.0)
    note: str = ""              # shown in UI: "Estimate; verify with institution"


class Education(BaseModel):
    degree: str                              # e.g. "B.Tech"
    field: str                               # e.g. "Computer Science"
    institution: str
    cgpa_value: Optional[float] = None
    cgpa_scale: Optional[float] = None       # e.g. 10.0
    normalized_gpa: Optional[NormalizedGPA] = None
    evidence_ref: Optional[EvidenceRef] = None


class Project(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    summary: str
    technologies: list[str] = Field(default_factory=list)
    source: str = "resume"   # "resume" | "github"
    evidence_ref: Optional[EvidenceRef] = None


class LanguageTest(BaseModel):
    test_name: str              # "IELTS", "TOEFL", "PTE", "Duolingo"
    overall_score: Optional[float] = None
    component_scores: Optional[dict[str, float]] = None   # e.g. {"reading": 8.0}
    evidence_ref: Optional[EvidenceRef] = None


class Preferences(BaseModel):
    countries: list[str] = Field(default_factory=list)
    degree_level: str = ""      # "masters", "phd", "any"
    target_fields: list[str] = Field(default_factory=list)
    funding_required: bool = True


class ProfileSignals(BaseModel):
    """Public signals fetched from a GitHub or similar public API."""
    projects: list[dict[str, Any]] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    activity_summary: str = ""


class CandidateProfile(BaseModel):
    """Scrubbed candidate profile; identity fields live only in PIIVault."""
    education: list[Education] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    language_tests: list[LanguageTest] = Field(default_factory=list)
    preferences: Preferences = Field(default_factory=Preferences)
    completeness_flags: dict[str, bool] = Field(default_factory=dict)
    github_signals: Optional[ProfileSignals] = None

    def primary_education(self) -> Optional[Education]:
        """Return the most recent education entry, if any."""
        return self.education[0] if self.education else None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. Search & Candidate models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SearchQuery(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    text: str
    intent: str   # "program" | "scholarship" | "funder"
    country: Optional[str] = None
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    depth: str = "basic"     # "basic" (1 credit) | "advanced" (2 credits)
    max_results: int = 10
    round_number: int = 0


class SearchPlan(BaseModel):
    queries: list[SearchQuery] = Field(default_factory=list)
    round_number: int = 0


class Candidate(BaseModel):
    """A raw URL candidate before auditing."""
    url: str
    canonical_url: str
    domain: str
    trust_tier: TrustTier = TrustTier.UNKNOWN
    kind_hint: Optional[OpportunityKind] = None
    source_query_id: str = ""
    title: Optional[str] = None
    snippet: Optional[str] = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Opportunity models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class OpportunityField(BaseModel):
    """A single extracted attribute with provenance."""
    field_name: str
    value: str
    evidence: Optional[EvidenceRef] = None
    status: FieldStatus = FieldStatus.UNVERIFIED


class FundingInfo(BaseModel):
    level: FundingLevel = FundingLevel.UNKNOWN
    details: str = ""
    amount: Optional[str] = None
    currency: Optional[str] = None
    evidence: Optional[EvidenceRef] = None
    status: FieldStatus = FieldStatus.UNVERIFIED


class DeadlineInfo(BaseModel):
    date: Optional[str] = None          # ISO date string or free text
    description: str = ""
    as_of: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    evidence: Optional[EvidenceRef] = None
    status: FieldStatus = FieldStatus.UNVERIFIED


class VerificationResult(BaseModel):
    """Result of the Auditor's existence check for one opportunity."""
    reachable: bool = False
    trust_tier: TrustTier = TrustTier.UNKNOWN
    existence_verified: bool = False    # reachable + name in page + ≥3 evidenced fields
    completeness_score: float = Field(0.0, ge=0.0, le=1.0)
    failure_notes: list[str] = Field(default_factory=list)


class Opportunity(BaseModel):
    """A verified or partially-verified study program or scholarship."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: OpportunityKind = OpportunityKind.PROGRAM
    name: str = ""
    institution_or_funder: str = ""
    country: str = ""
    degree_level: str = ""
    official_url: str = ""
    # Extracted attribute fields (name, deadline dates, etc.)
    fields: list[OpportunityField] = Field(default_factory=list)
    funding: Optional[FundingInfo] = None
    deadlines: list[DeadlineInfo] = Field(default_factory=list)
    requirements: list[OpportunityField] = Field(default_factory=list)
    verification: VerificationResult = Field(default_factory=lambda: VerificationResult())

    def is_existence_verified(self) -> bool:
        return self.verification.existence_verified

    def evidenced_field_count(self) -> int:
        return sum(1 for f in self.fields if f.status == FieldStatus.EVIDENCED)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. Eligibility & Assessment models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EligibilityRule(BaseModel):
    rule_name: str
    result: EligibilityResult
    candidate_value: Optional[str] = None
    required_value: Optional[str] = None
    evidence_ref: Optional[EvidenceRef] = None


class ActionableGap(BaseModel):
    gap_type: str            # e.g. "missing_language_test", "gpa_below_minimum"
    description: str
    remedy: str              # e.g. "Take IELTS and score ≥6.5"
    severity: GapSeverity
    blocking: bool = False   # True → caps composite score


class ComponentScore(BaseModel):
    component: str           # e.g. "academic_fit", "field_alignment"
    score: float = Field(ge=0.0, le=100.0)
    weight: float = Field(ge=0.0, le=1.0)
    explanation: str = ""


class EligibilityAssessment(BaseModel):
    opportunity_id: str
    rules: list[EligibilityRule] = Field(default_factory=list)
    gaps: list[ActionableGap] = Field(default_factory=list)
    component_scores: list[ComponentScore] = Field(default_factory=list)
    composite_score: float = Field(0.0, ge=0.0, le=100.0)
    rank: Optional[int] = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    rationale: str = ""      # cites evidence IDs; written by the LLM alignment call


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Application Package models
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ClaimLedgerEntry(BaseModel):
    """One fact from the allowed-fact pack used by Agent 5."""
    fact_id: str
    source: str              # "profile" | "opportunity"
    field_name: str
    value: str
    evidence_ref: Optional[EvidenceRef] = None


class SOPSentence(BaseModel):
    """One sentence in the SOP with its grounding."""
    text: str
    cited_fact_ids: list[str] = Field(default_factory=list)
    verified: bool = False


class ApplicationPackage(BaseModel):
    """Final deliverable from Agent 5 after claim-ledger verification."""
    sop_sentences: list[SOPSentence] = Field(default_factory=list)
    sop_text: str = ""                 # flat text with [CANDIDATE_NAME] placeholder
    word_count: int = 0
    claim_ledger: list[ClaimLedgerEntry] = Field(default_factory=list)
    execution_checklist: list[str] = Field(default_factory=list)
    target_opportunity_id: str = ""

    @model_validator(mode="after")
    def compute_word_count(self) -> "ApplicationPackage":
        if self.sop_text and self.word_count == 0:
            self.word_count = len(self.sop_text.split())
        return self


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. Run metadata & observability
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RunBudgets(BaseModel):
    """Call budgets; only decrease monotonically."""
    # Reasoning tier — DEC-1 hard cap
    reasoning_calls_used: int = 0
    reasoning_calls_limit: int = 4

    # Fast tier
    fast_calls_used: int = 0
    fast_calls_limit: int = 60   # conservative; 500 RPD but shared

    # Search credits (Tavily)
    search_credits_used: int = 0
    search_credits_limit: int = 30  # per run; protects monthly budget

    # Fetch count (informational)
    fetch_count: int = 0

    def reasoning_remaining(self) -> int:
        return max(0, self.reasoning_calls_limit - self.reasoning_calls_used)

    def fast_remaining(self) -> int:
        return max(0, self.fast_calls_limit - self.fast_calls_used)

    def search_remaining(self) -> int:
        return max(0, self.search_credits_limit - self.search_credits_used)


class RunDeadlines(BaseModel):
    """Immutable timing targets; injected at run start."""
    phase_a_soft_s: float = 60.0   # soft cutoff: stop new fetches (optimized for speed)
    phase_a_hard_s: float = 75.0   # hard stop for Phase A (optimized for speed)
    phase_b_cap_s: float = 45.0    # Phase B independent deadline (optimized for speed)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def elapsed_s(self) -> float:
        return (datetime.now(timezone.utc) - self.started_at).total_seconds()

    def phase_a_soft_reached(self) -> bool:
        return self.elapsed_s() >= self.phase_a_soft_s

    def phase_a_hard_reached(self) -> bool:
        return self.elapsed_s() >= self.phase_a_hard_s


class DiagnosticEntry(BaseModel):
    """Structured Scout retry diagnostics — no raw page text allowed."""
    round_number: int = 0
    failed_domains: list[str] = Field(default_factory=list)
    failed_status_codes: list[int] = Field(default_factory=list)
    under_delivering_intents: list[str] = Field(default_factory=list)
    missing_countries: list[str] = Field(default_factory=list)
    missing_degree_levels: list[str] = Field(default_factory=list)


class RunMeta(BaseModel):
    """Run-level metadata owned exclusively by the Run Engine."""
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    phase: RunPhase = RunPhase.INTAKE
    deadlines: RunDeadlines = Field(default_factory=RunDeadlines)
    budgets: RunBudgets = Field(default_factory=RunBudgets)
    retry_count: int = 0
    max_retries: int = 1
    degradation_flags: list[str] = Field(default_factory=list)
    outcome: RunOutcome = RunOutcome.IN_PROGRESS

    def is_terminal(self) -> bool:
        return self.outcome != RunOutcome.IN_PROGRESS

    def retry_allowed(self, time_remaining_s: float = 999.0) -> bool:
        return (
            self.retry_count < self.max_retries
            and time_remaining_s >= 25.0
            and not self.deadlines.phase_a_soft_reached()
        )


class Event(BaseModel):
    """Telemetry event published to the ring-buffer Event Bus."""
    run_id: str
    seq: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    node: str
    severity: EventSeverity = EventSeverity.INFO
    kind: EventKind
    payload: dict[str, Any] = Field(default_factory=dict)


class UsageRecord(BaseModel):
    """Written by the resilience layer; agents never write this directly."""
    provider: str
    model: str
    tier: LLMTier
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: float = 0.0
    outcome: str = "success"
    downshifted: bool = False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 8. SwarmState — the central blackboard
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SwarmState(BaseModel):
    """Central typed blackboard shared by all agents via the Run Engine.

    Ownership & merge semantics per §2.4 of the architecture report:
      - run_meta: Run Engine only; monotonic transitions
      - profile: Agent 1; written once
      - search_plans: Agent 2; append-by-round (max 2)
      - candidates: Agent 2; keyed set union by canonical_url
      - opportunities: Agent 3; keyed upsert with evidence promotion
      - diagnostics: Agent 3; append-only structured summary
      - assessments, top_5: Agent 4; single-write replace
      - application_package: Agent 5; staged replace after claim-ledger check
    """

    run_meta: RunMeta = Field(default_factory=RunMeta)
    vault_id: str = ""                   # handle to the per-run PIIVault; not the vault itself

    # Agent 1 output
    profile: Optional[CandidateProfile] = None

    # PIIVault reference for rehydration (stored as vault_id string)
    vault_reference: str = ""

    # Agent 2 output
    search_plans: list[SearchPlan] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)

    # Agent 3 output
    opportunities: list[Opportunity] = Field(default_factory=list)
    diagnostics: list[DiagnosticEntry] = Field(default_factory=list)

    # Temporary field for Send API mapper aggregation (Phase 2.1)
    # Uses operator.add reducer to aggregate results from parallel mappers
    verified_opps: Annotated[list[Opportunity], operator.add] = Field(default_factory=list)

    # Agent 4 output
    assessments: list[EligibilityAssessment] = Field(default_factory=list)
    top_5: list[str] = Field(default_factory=list)   # opportunity IDs, ranked

    # Agent 5 output
    application_package: Optional[ApplicationPackage] = None

    # Cross-cutting
    usage_records: list[UsageRecord] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    # ── Convenience queries ─────────────────────────────────────────────────

    def existence_verified_count(self) -> int:
        return sum(1 for o in self.opportunities if o.is_existence_verified())

    def top_5_opportunities(self) -> list[Opportunity]:
        opp_map = {o.id: o for o in self.opportunities}
        return [opp_map[oid] for oid in self.top_5 if oid in opp_map]

    def is_phase_a_done(self) -> bool:
        return self.run_meta.phase in (RunPhase.PHASE_B, RunPhase.COMPLETED)
