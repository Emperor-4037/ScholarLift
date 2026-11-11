"""Agent 4: Ranking Strategist — deterministic eligibility + scored top-5.

Rules and all displayed numbers are deterministic; the LLM only supplies
judgements (field alignment, profile strength, nationality/field judgement,
rationale ≤60 words, strengths, concerns) in ONE reasoning complete_json
(≤20 opps, overflow → fast). Fallback: keyword overlap + templated rationale.
"""
from __future__ import annotations

from typing import Any

import logging
from datetime import date

from scholarship_finder.domain import eligibility, scoring
from scholarship_finder.domain.models import (
    EligibilityAssessment,
    EventKind,
    EventSeverity,
    LLMTier,
    SwarmState,
    TrustTier,
)
from scholarship_finder.engine.context import emit
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.llm import LLMPort, TaskSpec

logger = logging.getLogger(__name__)

_PROMPT = """Judge each opportunity against the student. Return a JSON array,
one object per opportunity id in order:
[{{"id": "<id>", "field_alignment": 0-1, "profile_strength": 0-1,
"nationality_judgement": true|false|null, "field_judgement": true|false|null,
"rationale": "<=60 words citing profile + opportunity facts",
"strengths": [str], "concerns": [str]}}]

Student: {profile}

Opportunities:
{opps}"""


class RankingStrategistAgent:
    def __init__(self, llm_port: LLMPort) -> None:
        self._llm = llm_port

    async def run(self, state: SwarmState, bus: EventBus) -> dict:
        profile = state.profile
        if profile is None:
            emit("ranking_strategist", "No profile — skipping ranking")
            return {}
        opps = [o for o in state.opportunities
                if o.is_existence_verified()
                and o.verification.trust_tier != TrustTier.AGGREGATOR]
        if not opps:
            emit("ranking_strategist", "No verified opportunities to rank")
            return {}
        judgements = await self._judge(profile, opps)

        assessments: list[EligibilityAssessment] = []
        for opp in opps:
            j = judgements.get(opp.id, {})
            rules = eligibility.evaluate_rules(profile, opp, j)
            status = eligibility.classify_status(rules)
            days = _days(opp)
            margin = _gpa_margin(rules)
            ev_conf = opp.verification.completeness_score or 0.0
            composite, components = scoring.score_opportunity(
                opp, status=status, rules=rules,
                field_alignment=_num(j.get("field_alignment"), _kw(profile, opp)),
                profile_strength=_num(j.get("profile_strength"), 0.5),
                gpa_margin=margin, days_to_deadline=days,
                evidence_confidence=ev_conf,
            )
            assessments.append(EligibilityAssessment(
                opportunity_id=opp.id, status=status, rules=rules,
                component_scores=components, composite_score=composite,
                confidence=ev_conf,
                rationale=_text(j.get("rationale")) or _fallback_rationale(status, margin),
                strengths=_str_list(j.get("strengths")),
                concerns=_str_list(j.get("concerns")),
                days_to_deadline=days,
            ))
        top5 = scoring.select_top_five(assessments, opps,
                                       allowed_statuses=(
                                           "eligible", "likely_eligible",
                                           "near_miss", "requirements_unverified"),
                                       require_evidence=True)
        emit("ranking_strategist",
             f"Top {len(top5)} of {len(opps)} verified opportunities ranked",
             kind=EventKind.PHASE_A_READY,
             severity=EventSeverity.INFO if top5 else EventSeverity.WARNING,
             top_5=top5, opportunity_count=len(opps))
        return {"assessments": assessments, "top_5": top5}

    async def _judge(self, profile, opps) -> dict[str, dict]:
        block = "\n".join(
            f"- id={o.id} | {o.name} | {o.institution_or_funder} | "
            f"{o.country or o.host_countries} | levels={o.degree_levels or o.degree_level} | "
            f"fields={o.fields_of_study} | eligibility_text={o.eligibility.nationality_text or ''}"
            for o in opps[:20])
        try:
            res = await self._llm.complete_json(TaskSpec(
                json_schema={"type": "array", "items": {"type": "object"}},
                tier=LLMTier.REASONING,
                system=_PROMPT.format(profile=_brief(profile), opps=block),
                max_output_tokens=3000, purpose="alignment_scoring"))
            parsed = res.parsed
            items = parsed
            if isinstance(items, dict):
                items = next((v for v in items.values() if isinstance(v, list)), [])
            out: dict[str, dict] = {}
            for i, d in enumerate(items or []):
                if not isinstance(d, dict):
                    continue
                oid = (d.get("id") or d.get("opportunity_id")
                       or d.get("opp_id")
                       or (opps[i].id if i < len(opps) else None))
                if oid:
                    out[str(oid)] = d
            return out
        except Exception as exc:
            logger.warning("alignment parse failed (%r); raw=%r",
                           exc, getattr(res, "parsed", None) if "res" in dir() else None)
            emit("ranking_strategist",
                 f"LLM alignment failed ({type(exc).__name__}) — keyword fallback")
            return {}


def _brief(p) -> str:
    return (f"nationality={p.nationality or '?'} level={p.preferences.degree_level or '?'} "
            f"fields={p.study_fields()[:3]} countries={p.preferences.countries[:4]} "
            f"skills={p.skills[:8]} tests={[f'{t.test_name}:{t.overall_score}' for t in p.language_tests]}")


def _days(opp) -> int | None:
    dl = opp.next_deadline()
    if dl and dl.iso_date:
        try:
            return (date.fromisoformat(dl.iso_date[:10]) - date.today()).days
        except ValueError:
            return None
    return None


def _gpa_margin(rules) -> float | None:
    for r in rules:
        if r.rule_name == "gpa" and r.gap_size is not None:
            return -r.gap_size
        if r.rule_name == "gpa" and r.result.value == "MET":
            return 0.0
    return None


def _num(v: Any, d: float) -> float:
    return float(v) if isinstance(v, (int, float)) else d


def _text(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _str_list(v: Any) -> list[str]:
    """LLM lists may be a string, nested lists or contain non-strings."""
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    out: list[str] = []
    for item in v:
        if isinstance(item, list):
            out.extend(_str_list(item))
        elif isinstance(item, (str, int, float)) and str(item).strip():
            out.append(str(item).strip())
    return out[:6]


def _kw(profile, opp) -> float:
    st = set(" ".join(profile.study_fields()).lower().split())
    ot = set((opp.fields_of_study or [opp.degree_level or ""]).__str__().lower().split())
    return min(1.0, len(st & ot) / max(1, len(st))) * 0.7 + 0.3


def _fallback_rationale(status: str, margin: float | None) -> str:
    m = f" GPA margin {margin:+.2f}." if margin is not None else ""
    return f"Deterministic ranking (no LLM judgement). Status: {status}.{m}"
