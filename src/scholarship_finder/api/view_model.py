"""Build the UI view-model for a run — shared by routes.py and the CLI.

Everything returned here is safe to serialize to the browser: no raw page
content, no vault contents. SOP text is rehydrated with the candidate's
display name *for display only* (the stored sentence list keeps placeholders).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from scholarship_finder.domain.models import (
    ApplicationPackage,
    EligibilityAssessment,
    Opportunity,
    SwarmState,
)


def build_view_model(state: SwarmState, vault=None, finished: bool = False) -> dict[str, Any]:
    """Return the JSON-serialisable view-model for one run's state."""
    meta = state.run_meta

    return {
        "run_id": meta.run_id,
        "phase": meta.phase.value,
        "outcome": meta.outcome.value,
        "status": "finished" if finished or meta.outcome.value != "IN_PROGRESS" else "running",
        "elapsed_s": _elapsed(meta),
        "degradation_flags": meta.degradation_flags,
        "counters": {
            "candidates": len(state.candidates),
            "leads": len(state.leads),
            "opportunities": len(state.opportunities),
            "verified": state.existence_verified_count(),
            "assessed": len(state.assessments),
            "top_5": len(state.top_5),
            "search_rounds": len(state.search_plans),
            "expansion_rounds": meta.expansion_rounds,
        },
        "profile": _profile_view(state),
        "top_5": [_opp_card(state, oid, rank=i + 1) for i, oid in enumerate(state.top_5)],
        "other_opportunities": [_opp_row(state, o) for o in state.opportunities
                                if o.id not in set(state.top_5)],
        "improvement_plan": _plan_view(state),
        "applications": _apps_view(state, vault),
        "errors": state.errors[-20:],
        "leads": [{"name": l.name, "provider_hint": l.provider_hint,
                   "source_url": l.source_url} for l in state.leads[:30]],
    }


# ── sections ──────────────────────────────────────────────────────────────────

def _elapsed(meta) -> float:
    start = meta.deadlines.started_at
    end = meta.finished_at or datetime.now(timezone.utc)
    if start is None:
        return 0.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return round((end - start).total_seconds(), 1)


def _profile_view(state: SwarmState) -> dict[str, Any] | None:
    p = state.profile
    if p is None:
        return None
    edu = p.primary_education()
    return {
        "nationality": p.nationality or None,
        "current_country": p.current_country or None,
        "languages": p.languages,
        "education": {
            "degree": edu.degree if edu else "",
            "field": edu.field if edu else "",
            "institution": edu.institution if edu else "",
            "level": edu.level if edu else "",
            "cgpa": (f"{edu.cgpa_value}/{edu.cgpa_scale}"
                     if edu and edu.cgpa_value is not None else None),
            "us_gpa": p.us_gpa(),
        },
        "skills": p.skills[:20],
        "projects": [{"title": pr.title, "summary": pr.summary,
                      "technologies": pr.technologies, "source": pr.source}
                     for pr in p.projects[:10]],
        "research": [{"title": r.title, "kind": r.kind, "venue": r.venue}
                     for r in p.research[:6]],
        "experience": [{"role": e.role, "organization": e.organization,
                        "months": e.duration_months} for e in p.experience[:8]],
        "activities": [{"title": a.title, "role": a.role} for a in p.activities[:8]],
        "awards": [{"name": a.name, "year": a.year} for a in p.awards[:8]],
        "language_tests": [{"test": t.test_name, "score": t.overall_score}
                           for t in p.language_tests],
        "preferences": {
            "degree_level": p.preferences.degree_level,
            "target_fields": p.preferences.target_fields,
            "countries": p.preferences.countries,
            "instruction_languages": p.preferences.instruction_languages,
            "intake": p.preferences.intake,
            "funding_required": p.preferences.funding_required,
        },
        "financial_need": {"level": p.financial_need.level,
                           "annual_budget_usd": p.financial_need.annual_budget_usd},
        "completeness_flags": p.completeness_flags,
        "field_sources": p.field_sources,
    }


def _assessment_for(state: SwarmState, opp_id: str) -> EligibilityAssessment | None:
    return next((a for a in state.assessments if a.opportunity_id == opp_id), None)


def _deadline_view(opp: Opportunity) -> dict[str, Any] | None:
    dl = opp.next_deadline()
    if dl is None:
        return None
    days = None
    if dl.iso_date:
        try:
            days = (date.fromisoformat(dl.iso_date[:10]) - date.today()).days
        except ValueError:
            days = None
    return {"text": dl.date or "", "iso": dl.iso_date, "days_left": days,
            "is_past": dl.is_past}


def _opp_card(state: SwarmState, opp_id: str, rank: int) -> dict[str, Any]:
    opp = state.opportunity(opp_id)
    asmt = _assessment_for(state, opp_id)
    if opp is None:
        return {"id": opp_id, "rank": rank, "missing": True}
    return {
        "id": opp.id, "rank": rank,
        "name": opp.name,
        "provider": opp.institution_or_funder,
        "country": opp.country or ", ".join(opp.host_countries),
        "kind": opp.kind.value if opp.kind else "SCHOLARSHIP",
        "degree_levels": opp.degree_levels or ([opp.degree_level] if opp.degree_level else []),
        "fields_of_study": opp.fields_of_study,
        "official_url": opp.official_url,
        "application_url": opp.application_url,
        "how_to_apply": opp.how_to_apply,
        "trust_tier": opp.verification.trust_tier.value if opp.verification else "UNKNOWN",
        "funding": {
            "level": opp.funding.level.value if opp.funding and opp.funding.level else "UNKNOWN",
            "details": opp.funding.details if opp.funding else "",
            "amount": opp.funding.amount if opp.funding else None,
            "covers": opp.funding.covers if opp.funding else [],
        },
        "deadline": _deadline_view(opp),
        "selection_criteria": opp.selection_criteria[:8],
        "evidence": [
            {"field": f.field_name, "quote": f.evidence.span, "url": f.evidence.location}
            for f in opp.fields if f.evidence and f.evidence.span
        ],
        "assessment": _asmt_view(asmt),
        "application": {"status": state.applications[opp_id].status}
        if opp_id in state.applications else None,
    }


def _asmt_view(a: EligibilityAssessment | None) -> dict[str, Any] | None:
    if a is None:
        return None
    return {
        "status": a.status,
        "composite_score": a.composite_score,
        "confidence": a.confidence,
        "rank": a.rank,
        "days_to_deadline": a.days_to_deadline,
        "rationale": a.rationale,
        "strengths": a.strengths,
        "concerns": a.concerns,
        "components": [
            {"component": c.component, "score": c.score, "weight": c.weight,
             "weighted": c.weighted, "explanation": c.explanation}
            for c in a.component_scores
        ],
        "rules": [
            {"rule": r.rule_name, "label": r.label, "result": r.result.value,
             "candidate": r.candidate_value, "required": r.required_value,
             "explanation": r.explanation, "blocking": r.blocking, "closable": r.closable}
            for r in a.rules
        ],
        "gaps": _gaps_view(a),
    }


def _gaps_view(a: EligibilityAssessment | None) -> list[dict[str, Any]]:
    return [
        {"type": g.gap_type, "description": g.description, "remedy": g.remedy,
         "severity": getattr(g.severity, "value", str(g.severity)), "blocking": g.blocking}
        for g in (a.gaps if a else [])
    ]


def _opp_row(state: SwarmState, opp: Opportunity) -> dict[str, Any]:
    asmt = _assessment_for(state, opp.id)
    blk, gaps = [], []
    for r in (asmt.rules if asmt else []) or []:
        if r.result == "unmet" and r.blocking:
            blk.append(f"{r.label}: {r.explanation or r.required_value}")
        elif r.result == "unmet" and r.closable:
            gaps.append(f"{r.label}: {r.explanation or r.required_value}")
    reason = ("" if not asmt else {
        "ineligible": "ineligible — " + "; ".join(blk[:2]) if blk else "ineligible",
        "near_miss": "near miss — " + "; ".join(gaps[:2]) if gaps else "near miss",
        "requirements_unverified": "requirements not stated on page",
    }.get(asmt.status, ""))
    return {
        "id": opp.id, "name": opp.name,
        "provider": opp.institution_or_funder,
        "country": opp.country or ", ".join(opp.host_countries),
        "verified": opp.is_existence_verified(),
        "status": asmt.status if asmt else None,
        "score": asmt.composite_score if asmt else None,
        "blocking_reasons": blk,
        "near_miss_gaps": gaps,
        "gaps": _gaps_view(asmt),
        "not_ranked_reason": reason or (
            "not existence-verified" if not opp.is_existence_verified()
            else "not assessed"),
        "url": opp.official_url,
        "trust_tier": opp.verification.trust_tier.value if opp.verification else "UNKNOWN",
        "deadline": _deadline_view(opp),
    }


def _plan_view(state: SwarmState) -> dict[str, Any] | None:
    plan = state.improvement_plan
    if plan is None:
        return None
    return {
        "summary": plan.summary,
        "generated_by": plan.generated_by,
        "suggestions": [
            {"title": s.title, "action": s.action, "rationale": s.rationale,
             "category": s.category, "priority": s.priority,
             "unlocks": s.unlocks, "unlock_names": s.unlock_names,
             "deadline_hint": s.deadline_hint}
            for s in plan.suggestions
        ],
        "near_miss_ids": plan.near_miss_ids,
    }


def _app_view(pkg: ApplicationPackage, vault) -> dict[str, Any]:
    text = pkg.sop_text or ""
    if vault is not None and text:
        text = vault.rehydrate(text)   # display only; stored sentences keep placeholders
    return {
        "status": pkg.status,
        "sop_text": text,
        "word_count": pkg.word_count or len(text.split()),
        "claim_ledger": [
            {"fact_id": e.fact_id, "source": e.source,
             "field": e.field_name, "value": e.value}
            for e in pkg.claim_ledger
        ],
        "removed_claims": pkg.removed_claims,
        "execution_checklist": pkg.execution_checklist,
        "error": pkg.error or None,
    }


def _apps_view(state: SwarmState, vault) -> dict[str, Any]:
    return {oid: _app_view(pkg, vault) for oid, pkg in state.applications.items()}
