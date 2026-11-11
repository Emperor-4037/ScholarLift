"""Deterministic composite scoring — plan weights, explained in numbers.

Every component returns a 0–100 score plus a human explanation containing the
exact numbers used. The only LLM-sourced input is a judgement (field
alignment / profile strength); everything else is arithmetic.
"""
from __future__ import annotations

from scholarship_finder.domain.models import (
    ComponentScore,
    EligibilityResult,
    EligibilityRule,
    FundingLevel,
    Opportunity,
)

# Plan §3 Step E.5 weights
WEIGHTS: dict[str, float] = {
    "eligibility": 0.25,
    "field_alignment": 0.20,
    "funding_fit": 0.20,
    "academic_fit": 0.10,
    "profile_strength": 0.10,
    "evidence_confidence": 0.10,
    "deadline": 0.05,
}

_STATUS_SCORE = {
    "eligible": 100.0,
    "likely_eligible": 75.0,
    "near_miss": 45.0,
    "ineligible": 10.0,
}

_FUNDING_SCORE = {
    FundingLevel.FULL_TUITION_STIPEND: 100.0,
    FundingLevel.FULL_TUITION: 85.0,
    FundingLevel.PARTIAL: 55.0,
    FundingLevel.NONE: 10.0,
    FundingLevel.UNKNOWN: 40.0,
}


def _deadline_component(days: int | None) -> tuple[float, str]:
    if days is None:
        return 60.0, "no deadline stated → neutral 60"
    if days < 0:
        return 0.0, f"deadline passed {-days} days ago → 0"
    if days <= 14:
        return 30.0, f"deadline in {days}d (very tight) → 30"
    if days <= 90:
        return 80.0, f"deadline in {days}d → 80"
    return 100.0, f"deadline in {days}d (ample time) → 100"


def score_opportunity(
    opp: Opportunity,
    *,
    status: str,
    rules: list[EligibilityRule],
    field_alignment: float,
    profile_strength: float,
    gpa_margin: float | None,
    days_to_deadline: int | None,
    evidence_confidence: float,
) -> tuple[float, list[ComponentScore]]:
    """Return (composite_score 0–100, list[ComponentScore] with explanations)."""
    funding = opp.funding.level if opp.funding else FundingLevel.UNKNOWN

    elig_score = _STATUS_SCORE.get(status, 50.0)
    met = sum(1 for r in rules if r.result == EligibilityResult.MET)
    unmet = sum(1 for r in rules if r.result == EligibilityResult.UNMET)
    elig_expl = f"status={status} ({met} met / {unmet} unmet rules) → {elig_score:.0f}"

    fa = max(0.0, min(1.0, field_alignment)) * 100.0
    fa_expl = f"field alignment {field_alignment:.2f} → {fa:.0f}"

    fund = _FUNDING_SCORE.get(funding, 40.0)
    covers = f" covers {', '.join(opp.funding.covers)}" if (opp.funding and opp.funding.covers) else ""
    fund_expl = f"funding level {funding.value}{covers} → {fund:.0f}"

    if gpa_margin is None:
        acad, acad_expl = 50.0, "GPA not compared (missing data) → 50"
    else:
        acad = max(0.0, min(100.0, 65.0 + gpa_margin * 35.0))
        acad_expl = f"GPA margin {gpa_margin:+.2f} → {acad:.0f}"

    ps = max(0.0, min(1.0, profile_strength)) * 100.0
    ps_expl = f"profile strength {profile_strength:.2f} → {ps:.0f}"

    ec = max(0.0, min(1.0, evidence_confidence)) * 100.0
    ev_count = opp.evidenced_field_count()
    ec_expl = f"{ev_count} evidenced fields, completeness {evidence_confidence:.2f} → {ec:.0f}"

    dl, dl_expl = _deadline_component(days_to_deadline)

    parts = [
        ("eligibility", elig_score, elig_expl),
        ("field_alignment", fa, fa_expl),
        ("funding_fit", fund, fund_expl),
        ("academic_fit", acad, acad_expl),
        ("profile_strength", ps, ps_expl),
        ("evidence_confidence", ec, ec_expl),
        ("deadline", dl, dl_expl),
    ]
    components: list[ComponentScore] = []
    total = 0.0
    for name, score, expl in parts:
        w = WEIGHTS[name]
        weighted = score * w
        total += weighted
        components.append(ComponentScore(
            component=name, score=round(score, 2), weight=w,
            weighted=round(weighted, 2), explanation=expl,
        ))
    return round(total, 2), components


def select_top_five(
    assessments: list,
    opportunities: list[Opportunity],
    max_per_provider: int = 2,
    allowed_statuses: tuple = ("eligible", "likely_eligible", "near_miss",
                             "requirements_unverified"),
    require_evidence: bool = False,
) -> list[str]:
    """Top-5 by composite: eligible/likely first (≥1 evidenced field when
    require_evidence), ≤2/provider, then near_miss, then unverified — never
    ineligible."""
    provider_of = {o.id: (o.institution_or_funder or o.official_url) for o in opportunities}
    evd = {o.id: o.evidenced_field_count() for o in opportunities}
    def _ok(a):
        return (not require_evidence) or evd.get(a.opportunity_id, 0) >= 1
    ok = [a for a in assessments
          if a.status in ("eligible", "likely_eligible") and _ok(a)]
    near = [a for a in assessments if a.status == "near_miss" and _ok(a)]
    unv = [a for a in assessments
           if a.status == "requirements_unverified"]
    trust_of = {o.id: o.verification.trust_tier for o in opportunities}
    weak = [a for a in unv if str(trust_of.get(a.opportunity_id, ""))
            not in ("OFFICIAL_INSTITUTION", "RECOGNIZED_FUNDER")]
    unv = [a for a in unv if a not in weak]
    def _off_target(a) -> bool:
        blk = [r for r in (a.rules or [])
               if r.result == "unmet" and r.blocking]
        return a.status == "ineligible" and blk and all(
            r.rule_name == "destination" for r in blk)
    off = [a for a in assessments if _off_target(a) and _ok(a)]
    ranked = (sorted(ok, key=lambda a: -a.composite_score)
              + sorted(near, key=lambda a: -a.composite_score)
              + sorted(unv, key=lambda a: -a.composite_score)
              + sorted(weak, key=lambda a: -a.composite_score)
              + sorted(off, key=lambda a: -a.composite_score))
    counts: dict[str, int] = {}
    top: list[str] = []
    for a in ranked:
        if len(top) >= 5:
            break
        provider = provider_of.get(a.opportunity_id, "")
        if counts.get(provider, 0) >= max_per_provider:
            continue
        counts[provider] = counts.get(provider, 0) + 1
        top.append(a.opportunity_id)
    for i, oid in enumerate(top):
        for a in assessments:
            if a.opportunity_id == oid:
                a.rank = i + 1
    return top
