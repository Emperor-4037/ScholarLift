"""Deterministic weighted scoring engine for opportunity ranking.

Five components with configurable weights (architecture §2.4):
  academic_fit        25  — GPA margin vs stated minimum
  field_alignment     25  — LLM-judged 0–1 float (only LLM input to scoring)
  funding_strength    20  — based on FundingLevel enum
  feasibility         20  — language/test/deadline status composite
  evidence_confidence 10  — from VerificationResult.completeness_score

A blocking UNMET rule caps the composite score to 25.0.
Ties break on: evidence_confidence desc → deadline_distance asc.
Only the Top 5 are returned from rank_opportunities.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from scholarship_finder.domain.models import FundingLevel


DEFAULT_WEIGHTS: dict[str, float] = {
    "academic_fit": 0.25,
    "field_alignment": 0.25,
    "funding_strength": 0.20,
    "feasibility": 0.20,
    "evidence_confidence": 0.10,
}

BLOCKING_UNMET_CAP = 25.0   # composite score ceiling when a hard rule is UNMET


@dataclass
class ScoringInput:
    """All inputs needed to compute one opportunity's composite score."""
    gpa_margin: float | None        # how much candidate exceeds/misses minimum GPA
    field_alignment: float          # 0.0–1.0 from LLM fuzzy alignment judgment
    funding_level: FundingLevel
    feasibility_score: float        # 0.0–1.0 (language test, deadline, doc burden)
    evidence_confidence: float      # 0.0–1.0 from VerificationResult.completeness_score
    has_blocking_unmet: bool = False
    deadline_distance_days: int | None = None   # days to deadline; None = unknown


@dataclass
class CompositeScore:
    total: float                         # 0.0–100.0
    components: dict[str, float] = field(default_factory=dict)  # component_name → 0–100 score
    capped: bool = False
    cap_reason: str | None = None
    evidence_confidence: float = 0.0     # exposed for tiebreaking
    deadline_distance_days: int | None = None


# ── Component scorers ─────────────────────────────────────────────────────────

def compute_academic_fit(gpa_margin: float | None) -> float:
    """0–100 score for GPA fit; None → neutral 50 (UNKNOWN)."""
    if gpa_margin is None:
        return 50.0
    # Map margin to score: +1.0 → 100, 0.0 → 65, -0.5 → 0 (linear)
    if gpa_margin >= 1.0:
        return 100.0
    if gpa_margin >= 0.0:
        return 65.0 + gpa_margin * 35.0
    # Below minimum — fall quickly
    score = 65.0 + gpa_margin * 130.0   # deficit of 0.5 → score ≈ 0
    return max(0.0, score)


def compute_funding_score(level: FundingLevel) -> float:
    """0–100 score mapping FundingLevel to a numeric reward."""
    table = {
        FundingLevel.FULL_TUITION_STIPEND: 100.0,
        FundingLevel.FULL_TUITION: 80.0,
        FundingLevel.PARTIAL: 50.0,
        FundingLevel.NONE: 20.0,
        FundingLevel.UNKNOWN: 40.0,  # neutral; not penalised as hard as NONE
    }
    return table.get(level, 40.0)


def compute_composite_score(
    inp: ScoringInput,
    weights: dict[str, float] | None = None,
) -> CompositeScore:
    """Return the composite score for a single opportunity.

    Only the `field_alignment` component originates from an LLM call.
    Everything else is arithmetic.
    """
    w = weights or DEFAULT_WEIGHTS

    components: dict[str, float] = {
        "academic_fit": compute_academic_fit(inp.gpa_margin),
        "field_alignment": max(0.0, min(100.0, inp.field_alignment * 100.0)),
        "funding_strength": compute_funding_score(inp.funding_level),
        "feasibility": max(0.0, min(100.0, inp.feasibility_score * 100.0)),
        "evidence_confidence": max(0.0, min(100.0, inp.evidence_confidence * 100.0)),
    }

    total = sum(components[k] * w.get(k, 0.0) for k in components)
    total = max(0.0, min(100.0, total))

    capped = False
    cap_reason: str | None = None
    if inp.has_blocking_unmet and total > BLOCKING_UNMET_CAP:
        total = BLOCKING_UNMET_CAP
        capped = True
        cap_reason = f"Blocked by hard UNMET eligibility rule; score capped at {BLOCKING_UNMET_CAP}"

    return CompositeScore(
        total=round(total, 2),
        components={k: round(v, 2) for k, v in components.items()},
        capped=capped,
        cap_reason=cap_reason,
        evidence_confidence=inp.evidence_confidence,
        deadline_distance_days=inp.deadline_distance_days,
    )


def rank_opportunities(
    scores: list[tuple[str, CompositeScore]],
) -> list[tuple[str, CompositeScore, int]]:
    """Sort opportunity scores and return ranked Top-5 tuples.

    Sort key: (total desc, evidence_confidence desc, deadline_distance_days asc).
    A None deadline sorts after any numeric deadline (treated as very far away).

    Args:
        scores: List of (opportunity_id, CompositeScore) pairs.

    Returns:
        List of (opportunity_id, score, rank) — at most 5 entries, 1-indexed.
    """
    def sort_key(item: tuple[str, CompositeScore]) -> tuple[float, float, int]:
        _, s = item
        dd = s.deadline_distance_days if s.deadline_distance_days is not None else 99999
        return (-s.total, -s.evidence_confidence, dd)

    sorted_scores = sorted(scores, key=sort_key)
    return [
        (opp_id, score, rank + 1)
        for rank, (opp_id, score) in enumerate(sorted_scores[:5])
    ]
