"""All domain models for ScholarLift.

Kept in one module to avoid Pydantic v2 forward-reference complications.
Sections:
  1. Enums
  2. Evidence & student profile
  3. Intake
  4. Search & candidates
  5. Opportunities
  6. Eligibility, assessment & improvement plan
  7. Application package (SOP)
  8. Run metadata & observability
  9. SwarmState (the shared blackboard)
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


def _uid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. Enums
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RunOutcome(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    DEGRADED = "DEGRADED"       # finished, but with gaps (few opportunities, no SOP, timeout)
    PARTIAL = "PARTIAL"         # legacy alias kept for compatibility
    FAILED_SAFE = "FAILED_SAFE"  # unrecoverable error; partial state preserved
    CANCELLED = "CANCELLED"


class RunPhase(str, enum.Enum):
    INTAKE = "INTAKE"
    PHASE_A = "PHASE_A"     # discovery: profile → search → audit → rank
    PHASE_B = "PHASE_B"     # advice + SOP
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
    RECOGNIZED_FUNDER = "RECOGNIZED_FUNDER"        # daad.de, chevening.org, ...
    AGGREGATOR = "AGGREGATOR"                      # listing portals (leads only)
    UNKNOWN = "UNKNOWN"


class GapSeverity(str, enum.Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class EventKind(str, enum.Enum):
    RUN_STARTED = "run_started"
    NODE_STARTED = "node_started"
    PROGRESS = "progress"
    WARNING = "warning"
    RETRY_TRIGGERED = "retry_triggered"
    NODE_COMPLETED = "node_completed"
    PHASE_A_READY = "phase_a_ready"
    SOP_READY = "sop_ready"
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


AssessmentStatus = Literal["eligible", "likely_eligible", "near_miss", "ineligible", "requirements_unverified"]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. Evidence & student profile
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EvidenceRef(BaseModel):
    """Points to where a claim was sourced (document page, web page, API)."""
    source: str                     # "web", "doc", "github", "form"
    location: str                   # URL, page number, repo slug ...
    span: Optional[str] = None      # verbatim text (15–400 chars) for web evidence
    offset: Optional[int] = None
    fetched_at: Optional[datetime] = None

    @field_validator("span")
    @classmethod
    def validate_span_length(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not (15 <= len(v) <= 400):
            raise ValueError(f"Evidence span length must be 15–400 chars, got {len(v)}")
        return v


class NormalizedGPA(BaseModel):
    """GPA converted to the US 4.0 scale; always an estimate."""
    value: float
    target_scale: str = "us_4point"
    method: str = ""
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    note: str = ""


class Education(BaseModel):
    degree: str = ""                         # e.g. "B.Tech"
    field: str = ""                          # e.g. "Computer Science"
    institution: str = ""
    level: str = ""                          # bachelors | masters | phd | diploma | school
    country: str = ""
    graduation_year: Optional[int] = None
    cgpa_value: Optional[float] = None
    cgpa_scale: Optional[float] = None       # e.g. 10.0, 4.0, 100.0
    grade_system: str = ""                   # "" or "lower_is_better" (e.g. German 1.0 best)
    normalized_gpa: Optional[NormalizedGPA] = None
    evidence_ref: Optional[EvidenceRef] = None


class Project(BaseModel):
    id: str = Field(default_factory=_uid)
    title: str
    summary: str = ""
    technologies: list[str] = Field(default_factory=list)
    url: str = ""
    source: str = "document"   # "document" | "form" | "github"
    evidence_ref: Optional[EvidenceRef] = None


class LanguageTest(BaseModel):
    test_name: str              # "IELTS", "TOEFL", "PTE", "Duolingo", "GRE", ...
    overall_score: Optional[float] = None
    component_scores: Optional[dict[str, float]] = None
    evidence_ref: Optional[EvidenceRef] = None


class ResearchItem(BaseModel):
    title: str
    kind: str = "research_project"   # publication | thesis | research_project | patent
    venue: str = ""
    year: Optional[int] = None
    summary: str = ""
    source: str = "document"


class ExperienceItem(BaseModel):
    role: str
    organization: str = ""
    kind: str = "work"               # work | internship | volunteer
    duration_months: Optional[int] = None
    summary: str = ""
    source: str = "document"


class ActivityItem(BaseModel):
    title: str
    role: str = ""
    summary: str = ""
    source: str = "document"


class AwardItem(BaseModel):
    name: str
    year: Optional[int] = None
    summary: str = ""
    is_scholarship: bool = False
    source: str = "document"


class FinancialNeed(BaseModel):
    level: Literal["full", "partial", "none", "unknown"] = "unknown"
    annual_budget_usd: Optional[float] = None
    context: str = ""


class Preferences(BaseModel):
    countries: list[str] = Field(default_factory=list)   # countries or regions
    degree_level: str = ""                               # target: masters | phd | bachelors ...
    target_fields: list[str] = Field(default_factory=list)
    funding_required: bool = True
    instruction_languages: list[str] = Field(default_factory=list)
    intake: str = ""                                     # e.g. "Fall 2027"


class ProfileSignals(BaseModel):
    """Public signals fetched from GitHub (or a similar public source)."""
    projects: list[dict[str, Any]] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    activity_summary: str = ""


class CandidateProfile(BaseModel):
    """Scrubbed student profile; identity fields (name, email, phone) live only in PIIVault."""
    current_level: str = ""                 # e.g. "final-year undergraduate"
    nationality: str = ""
    current_country: str = ""
    languages: list[str] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    research: list[ResearchItem] = Field(default_factory=list)
    experience: list[ExperienceItem] = Field(default_factory=list)
    activities: list[ActivityItem] = Field(default_factory=list)
    awards: list[AwardItem] = Field(default_factory=list)
    language_tests: list[LanguageTest] = Field(default_factory=list)
    financial_need: FinancialNeed = Field(default_factory=FinancialNeed)
    preferences: Preferences = Field(default_factory=Preferences)
    github_signals: Optional[ProfileSignals] = None
    field_sources: dict[str, str] = Field(default_factory=dict)   # field → form|document|github
    completeness_flags: dict[str, bool] = Field(default_factory=dict)

    def primary_education(self) -> Optional[Education]:
        """Most relevant education entry: first with a GPA, else the first entry."""
        for e in self.education:
            if e.cgpa_value is not None:
                return e
        return self.education[0] if self.education else None

    def us_gpa(self) -> Optional[float]:
        edu = self.primary_education()
        return edu.normalized_gpa.value if edu and edu.normalized_gpa else None

    def total_experience_months(self) -> int:
        return sum(e.duration_months or 0 for e in self.experience)

    def study_fields(self) -> list[str]:
        """Target fields, falling back to fields of past study."""
        fields = list(self.preferences.target_fields)
        fields += [e.field for e in self.education if e.field and e.field not in fields]
        return fields


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. Intake
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ScrubbedDocument(BaseModel):
    label: str = "RESUME"        # RESUME | TRANSCRIPT | OTHER
    filename: str = ""
    text: str = ""               # PII-scrubbed text
    pages: int = 1
    warnings: list[str] = Field(default_factory=list)


class IntakeBundle(BaseModel):
    """Everything the student supplied, after PII scrubbing."""
    documents: list[ScrubbedDocument] = Field(default_factory=list)
    form_profile: Optional[CandidateProfile] = None
    github_username: Optional[str] = None
    has_display_name: bool = False
    warnings: list[str] = Field(default_factory=list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. Search & candidates
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SearchQuery(BaseModel):
    id: str = Field(default_factory=_uid)
    text: str
    intent: str = "university_scholarship"
    country: Optional[str] = None
    include_domains: list[str] = Field(default_factory=list)
    exclude_domains: list[str] = Field(default_factory=list)
    depth: str = "basic"     # "basic" (1 credit) | "advanced" (2 credits)
    max_results: int = 8
    round_number: int = 0


class SearchPlan(BaseModel):
    queries: list[SearchQuery] = Field(default_factory=list)
    round_number: int = 0
    planner: str = "llm"     # "llm" | "fallback"


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
    prior_score: float = 0.0
    origin: str = "search"   # "search" | "lead"
    country_hint: Optional[str] = None
    audited: bool = False


class Lead(BaseModel):
    """A named opportunity seen on a listing page; needs its official page found."""
    name: str
    provider_hint: str = ""
    source_url: str = ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 5. Opportunities
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
    amount: Optional[str] = None          # free text as stated
    amount_value: Optional[float] = None
    currency: Optional[str] = None
    period: Optional[str] = None          # "per month", "per year", "total"
    covers: list[str] = Field(default_factory=list)
    evidence: Optional[EvidenceRef] = None
    status: FieldStatus = FieldStatus.UNVERIFIED


class DeadlineInfo(BaseModel):
    date: Optional[str] = None            # as stated on the page
    iso_date: Optional[str] = None        # YYYY-MM-DD when parseable
    is_past: Optional[bool] = None
    description: str = ""
    as_of: datetime = Field(default_factory=_now)
    evidence: Optional[EvidenceRef] = None
    status: FieldStatus = FieldStatus.UNVERIFIED


class LanguageRequirement(BaseModel):
    test: str
    min_score: float
    text: str = ""


class EligibilityCriteria(BaseModel):
    nationalities: list[str] = Field(default_factory=list)   # explicit list; empty = not stated
    nationality_text: str = ""                               # e.g. "citizens of DAC-list countries"
    open_to_international: Optional[bool] = None
    degree_levels: list[str] = Field(default_factory=list)
    fields_of_study: list[str] = Field(default_factory=list)  # empty = any / not stated
    min_gpa_value: Optional[float] = None
    min_gpa_scale: Optional[float] = None
    min_gpa_text: str = ""
    language_requirements: list[LanguageRequirement] = Field(default_factory=list)
    min_work_experience_months: Optional[int] = None
    max_age: Optional[int] = None
    other: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    """Result of the Auditor's existence check for one opportunity."""
    reachable: bool = False
    trust_tier: TrustTier = TrustTier.UNKNOWN
    existence_verified: bool = False
    completeness_score: float = Field(0.0, ge=0.0, le=1.0)
    evidenced_fields: int = 0
    failure_notes: list[str] = Field(default_factory=list)


class Opportunity(BaseModel):
    """A verified (or partially verified) study program or scholarship."""
    id: str = Field(default_factory=_uid)
    kind: OpportunityKind = OpportunityKind.SCHOLARSHIP
    name: str = ""
    institution_or_funder: str = ""
    country: str = ""                       # display string (host countries joined)
    host_countries: list[str] = Field(default_factory=list)
    degree_level: str = ""                  # display string
    degree_levels: list[str] = Field(default_factory=list)
    fields_of_study: list[str] = Field(default_factory=list)
    official_url: str = ""
    application_url: str = ""
    how_to_apply: str = ""
    selection_criteria: list[str] = Field(default_factory=list)
    fields: list[OpportunityField] = Field(default_factory=list)   # evidenced attributes
    funding: Optional[FundingInfo] = None
    deadlines: list[DeadlineInfo] = Field(default_factory=list)
    eligibility: EligibilityCriteria = Field(default_factory=EligibilityCriteria)
    requirements: list[OpportunityField] = Field(default_factory=list)
    verification: VerificationResult = Field(default_factory=VerificationResult)
    origin: str = "search"

    def is_existence_verified(self) -> bool:
        return self.verification.existence_verified

    def evidenced_field_count(self) -> int:
        return sum(1 for f in self.fields if f.status == FieldStatus.EVIDENCED)

    def evidence_for(self, field_name: str) -> Optional[OpportunityField]:
        return next((f for f in self.fields if f.field_name == field_name), None)

    def next_deadline(self) -> Optional[DeadlineInfo]:
        upcoming = [d for d in self.deadlines if d.iso_date and not d.is_past]
        if upcoming:
            return sorted(upcoming, key=lambda d: d.iso_date or "")[0]
        return self.deadlines[0] if self.deadlines else None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 6. Eligibility, assessment & improvement plan
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EligibilityRule(BaseModel):
    rule_name: str
    label: str = ""
    result: EligibilityResult
    candidate_value: Optional[str] = None
    required_value: Optional[str] = None
    explanation: str = ""
    blocking: bool = False      # UNMET here makes the student ineligible
    closable: bool = False      # the student can realistically fix this gap
    gap_size: Optional[float] = None
    evidence_ref: Optional[EvidenceRef] = None


class ActionableGap(BaseModel):
    gap_type: str
    description: str
    remedy: str
    severity: GapSeverity = GapSeverity.MEDIUM
    blocking: bool = False


class ComponentScore(BaseModel):
    component: str
    score: float = Field(ge=0.0, le=100.0)
    weight: float = Field(ge=0.0, le=1.0)
    weighted: float = 0.0
    explanation: str = ""


class EligibilityAssessment(BaseModel):
    opportunity_id: str
    status: AssessmentStatus = "likely_eligible"
    rules: list[EligibilityRule] = Field(default_factory=list)
    gaps: list[ActionableGap] = Field(default_factory=list)
    component_scores: list[ComponentScore] = Field(default_factory=list)
    composite_score: float = Field(0.0, ge=0.0, le=100.0)
    rank: Optional[int] = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    rationale: str = ""
    strengths: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    days_to_deadline: Optional[int] = None


class Suggestion(BaseModel):
    id: str = Field(default_factory=_uid)
    title: str
    action: str
    rationale: str = ""
    category: str = "profile"   # test | academic | experience | research | documents | profile
    priority: int = Field(3, ge=1, le=5)    # 1 = do first
    effort: str = ""                        # "days" | "weeks" | "months"
    unlocks: list[str] = Field(default_factory=list)       # opportunity IDs
    unlock_names: list[str] = Field(default_factory=list)
    deadline_hint: str = ""


class ImprovementPlan(BaseModel):
    summary: str = ""
    suggestions: list[Suggestion] = Field(default_factory=list)
    near_miss_ids: list[str] = Field(default_factory=list)
    generated_by: str = "llm"   # "llm" | "fallback"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 7. Application package
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ClaimLedgerEntry(BaseModel):
    """One fact from the allowed-fact pack used to write the SOP."""
    fact_id: str
    source: str              # "profile" | "opportunity"
    field_name: str
    value: str
    evidence_ref: Optional[EvidenceRef] = None


class SOPSentence(BaseModel):
    text: str                                    # display text, citations stripped
    cited_fact_ids: list[str] = Field(default_factory=list)
    verified: bool = False
    repaired: bool = False


class ApplicationPackage(BaseModel):
    """SOP + checklist for one opportunity, after claim-ledger verification."""
    target_opportunity_id: str = ""
    status: Literal["generating", "ready", "failed"] = "ready"
    sop_sentences: list[SOPSentence] = Field(default_factory=list)
    sop_text: str = ""                 # flat text with [CANDIDATE_NAME] placeholder
    word_count: int = 0
    claim_ledger: list[ClaimLedgerEntry] = Field(default_factory=list)
    removed_claims: list[str] = Field(default_factory=list)
    execution_checklist: list[str] = Field(default_factory=list)
    error: str = ""

    @model_validator(mode="after")
    def compute_word_count(self) -> "ApplicationPackage":
        if self.sop_text and self.word_count == 0:
            self.word_count = len(self.sop_text.split())
        return self


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 8. Run metadata & observability
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RunDeadlines(BaseModel):
    soft_s: float = 150.0      # stop launching new discovery work
    hard_s: float = 270.0      # hard stop for the whole run
    started_at: datetime = Field(default_factory=_now)

    def elapsed_s(self) -> float:
        return (_now() - self.started_at).total_seconds()


class RunMeta(BaseModel):
    """Run-level metadata."""
    run_id: str = Field(default_factory=_uid)
    phase: RunPhase = RunPhase.INTAKE
    deadlines: RunDeadlines = Field(default_factory=RunDeadlines)
    expansion_rounds: int = 0
    degradation_flags: list[str] = Field(default_factory=list)
    outcome: RunOutcome = RunOutcome.IN_PROGRESS
    finished_at: Optional[datetime] = None

    def is_terminal(self) -> bool:
        return self.outcome != RunOutcome.IN_PROGRESS


class Event(BaseModel):
    """Telemetry event published to the ring-buffer Event Bus."""
    run_id: str
    seq: int = 0
    timestamp: datetime = Field(default_factory=_now)
    node: str
    severity: EventSeverity = EventSeverity.INFO
    kind: EventKind
    payload: dict[str, Any] = Field(default_factory=dict)


class UsageRecord(BaseModel):
    """One physical LLM call (written by the LLM router)."""
    provider: str
    model: str
    tier: LLMTier
    purpose: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: float = 0.0
    outcome: str = "success"
    downshifted: bool = False


class DiagnosticEntry(BaseModel):
    """Structured per-stage diagnostics (no raw page text)."""
    stage: str = ""
    round_number: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    failed_domains: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 9. SwarmState
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SwarmState(BaseModel):
    """Shared typed blackboard. Graph nodes return partial dict updates of these keys."""

    run_meta: RunMeta = Field(default_factory=RunMeta)
    vault_id: str = ""                    # handle to the per-run PIIVault (never the vault)
    intake: IntakeBundle = Field(default_factory=IntakeBundle)

    profile: Optional[CandidateProfile] = None                    # Profile Enricher
    search_plans: list[SearchPlan] = Field(default_factory=list)  # Scout
    candidates: list[Candidate] = Field(default_factory=list)     # Scout
    leads: list[Lead] = Field(default_factory=list)               # Auditor → Scout
    opportunities: list[Opportunity] = Field(default_factory=list)  # Auditor
    assessments: list[EligibilityAssessment] = Field(default_factory=list)  # Ranking
    top_5: list[str] = Field(default_factory=list)                # Ranking (opportunity IDs)
    improvement_plan: Optional[ImprovementPlan] = None            # Gap Advisor
    applications: dict[str, ApplicationPackage] = Field(default_factory=dict)  # Strategist

    diagnostics: list[DiagnosticEntry] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    # ── Convenience queries ─────────────────────────────────────────────────

    def verified_opportunities(self) -> list[Opportunity]:
        return [o for o in self.opportunities if o.is_existence_verified()]

    def existence_verified_count(self) -> int:
        return len(self.verified_opportunities())

    def opportunity(self, opp_id: str) -> Optional[Opportunity]:
        return next((o for o in self.opportunities if o.id == opp_id), None)

    def assessment(self, opp_id: str) -> Optional[EligibilityAssessment]:
        return next((a for a in self.assessments if a.opportunity_id == opp_id), None)

    def top_5_opportunities(self) -> list[Opportunity]:
        opp_map = {o.id: o for o in self.opportunities}
        return [opp_map[oid] for oid in self.top_5 if oid in opp_map]

    def primary_application(self) -> Optional[ApplicationPackage]:
        return self.applications.get(self.top_5[0]) if self.top_5 else None
