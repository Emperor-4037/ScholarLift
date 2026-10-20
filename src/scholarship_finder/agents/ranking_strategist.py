"""Agent 4: Ranking Strategist

Responsibilities (architecture §1.3, Agent 4):
- Deterministic tri-state eligibility rule evaluation (no LLM)
- Gap derivation (UNKNOWN/UNMET → actionable gaps with severity)
- ONE batched reasoning-tier LLM call for field-alignment scoring
  (0-1 score: how well does each opportunity match the candidate's field?)
- Weighted composite scoring via scoring.py
- Rank top-5 opportunities by composite score
- Return updated SwarmState with assessments + top_5

DEC-1: reasoning tier capped at ≤4 per run. This agent uses 1 call.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from scholarship_finder.domain.models import (
    ActionableGap,
    ComponentScore,
    EligibilityAssessment,
    EligibilityRule,
    Event,
    EventKind,
    EventSeverity,
    FundingLevel,
    GapSeverity,
    LLMTier,
    Opportunity,
    SwarmState,
)
from scholarship_finder.domain.reducers import reduce_assessments
from scholarship_finder.domain.scoring import (
    CompositeScore,
    ScoringInput,
    compute_composite_score,
    rank_opportunities,
)
from scholarship_finder.domain.tri_state import (
    derive_gaps,
    evaluate_citizenship,
    evaluate_degree_field,
    evaluate_degree_level,
    evaluate_document_availability,
    evaluate_gpa_minimum,
    evaluate_language_test,
    has_blocking_unmet,
)
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.llm import LLMPort, TaskSpec

logger = logging.getLogger(__name__)

_ALIGNMENT_PROMPT = """\
You are a graduate admissions expert. For each opportunity below, score how \
well the candidate's academic background aligns with the opportunity's field \
of study, on a scale of 0.0 to 1.0.

Return ONLY valid JSON array (no markdown):
[
  {"opportunity_id": "<id>", "field_alignment": <float 0.0-1.0>, "rationale": "<1 sentence>"},
  ...
]

Candidate fields of interest: {candidate_fields}
Candidate skills: {candidate_skills}

Opportunities:
{opps_block}
"""


class RankingStrategistAgent:
    """Agent 4: Evaluates eligibility, aligns fields, scores, and ranks opportunities."""

    def __init__(self, llm_port: LLMPort) -> None:
        self._llm = llm_port

    async def run(self, state: SwarmState, bus: EventBus) -> SwarmState:
        """Rank opportunities and return updated state with assessments + top_5."""
        run_id = state.run_meta.run_id
        profile = state.profile
        opportunities = state.opportunities

        if not profile or not opportunities:
            logger.warning("RankingStrategist: missing profile or opportunities")
            return state

        await bus.publish(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="ranking_strategist",
            severity=EventSeverity.INFO,
            kind=EventKind.NODE_STARTED,
            payload={"node": "ranking_strategist", "opp_count": len(opportunities)},
        ))

        # ── Step 1: Deterministic tri-state eligibility (no LLM) ─────────────
        rule_results_by_opp = self._evaluate_eligibility(profile, opportunities)

        # ── Step 2: ONE reasoning-tier alignment call (batched) ───────────────
        alignment_scores = await self._get_alignment_scores(profile, opportunities, run_id)

        # ── Step 3: Weighted scoring ──────────────────────────────────────────
        scored: list[tuple[str, CompositeScore]] = []
        for opp in opportunities:
            rule_results = rule_results_by_opp.get(opp.id, [])
            blocking = has_blocking_unmet(rule_results)

            gpa_margin = self._compute_gpa_margin(profile, opp)
            field_align = alignment_scores.get(opp.id, 0.5)

            evidence_conf = opp.verification.completeness_score if opp.verification else 0.5
            deadline_days = self._deadline_distance(opp)

            inp = ScoringInput(
                gpa_margin=gpa_margin,
                field_alignment=field_align,
                funding_level=(opp.funding.level if opp.funding else None) or FundingLevel.UNKNOWN,
                feasibility_score=0.7 if not blocking else 0.2,
                evidence_confidence=evidence_conf,
                has_blocking_unmet=blocking,
                deadline_distance_days=deadline_days,
            )
            score = compute_composite_score(inp)
            scored.append((opp.id, score))

        # ── Step 4: Rank and select top-5 ────────────────────────────────────
        ranked = rank_opportunities(scored)
        top_5_ids = [opp_id for opp_id, _, _ in ranked[:5]]

        # ── Step 5: Build EligibilityAssessment objects ───────────────────────
        assessments = self._build_assessments(
            opportunities, rule_results_by_opp, scored, ranked, alignment_scores
        )

        new_state = state.model_copy(deep=True)
        new_assessments, new_top5 = reduce_assessments(
            state.assessments, assessments, top_5_ids
        )
        new_state.assessments = new_assessments
        new_state.top_5 = new_top5

        await bus.publish(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="ranking_strategist",
            severity=EventSeverity.INFO,
            kind=EventKind.NODE_COMPLETED,
            payload={
                "node": "ranking_strategist",
                "top_5": top_5_ids,
                "reasoning_calls_used": state.run_meta.budgets.reasoning_calls_used + 1,
            },
        ))

        return new_state

    # ── Private helpers ───────────────────────────────────────────────────────

    def _evaluate_eligibility(self, profile: Any, opportunities: list[Opportunity]) -> dict:
        """Run deterministic tri-state rules for each opportunity."""
        results = {}

        # Extract candidate values once
        candidate_gpa: float | None = None
        candidate_fields: list[str] = []
        candidate_lang_score: float | None = None

        for edu in (profile.education or []):
            if edu.normalized_gpa and candidate_gpa is None:
                candidate_gpa = edu.normalized_gpa.value
            if edu.field:
                candidate_fields.append(edu.field)

        for lt in (profile.language_tests or []):
            if lt.overall_score is not None:
                candidate_lang_score = lt.overall_score
                break

        candidate_degree_level = getattr(profile.preferences, "degree_level", None)

        for opp in opportunities:
            opp_fields = [opp.degree_level] if opp.degree_level else []
            results[opp.id] = [
                evaluate_gpa_minimum(candidate_gpa, None),           # required GPA unknown from page
                evaluate_degree_field(candidate_fields, opp_fields),
                evaluate_language_test(candidate_lang_score, None),  # required score unknown
                evaluate_degree_level(candidate_degree_level, opp.degree_level),
                evaluate_citizenship(),
                evaluate_document_availability(),
            ]

        return results

    async def _get_alignment_scores(
        self, profile: Any, opportunities: list[Opportunity], run_id: str
    ) -> dict[str, float]:
        """ONE reasoning-tier LLM call to score field alignment for all opps."""
        if not opportunities:
            return {}

        prefs = profile.preferences
        candidate_fields = ", ".join(prefs.target_fields[:5]) if prefs.target_fields else "general"
        candidate_skills = ", ".join(profile.skills[:10]) if profile.skills else "none"

        opps_block = "\n".join(
            f"- ID: {opp.id[:8]} | Name: {opp.name} | Field: {opp.degree_level} | "
            f"Country: {opp.country} | Funding: {opp.funding.level.value if opp.funding else 'unknown'}"
            for opp in opportunities[:20]  # batch cap
        )

        task = TaskSpec(
            tier=LLMTier.REASONING,
            instruction=_ALIGNMENT_PROMPT.format(
                candidate_fields=candidate_fields,
                candidate_skills=candidate_skills,
                opps_block=opps_block,
            ),
            data_blocks=[],
            output_schema=None,
            max_output_tokens=2000,
            reasoning_effort="low",  # minimize tokens for this batch
        )

        try:
            result = await self._llm.complete(task)
            raw = result.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw)
            scores = {}
            for item in data:
                opp_id_prefix = item.get("opportunity_id", "")
                score = float(item.get("field_alignment", 0.5))
                # Match by prefix since we truncated ID to 8 chars in the prompt
                for opp in opportunities:
                    if opp.id.startswith(opp_id_prefix) or opp.id == opp_id_prefix:
                        scores[opp.id] = max(0.0, min(1.0, score))
                        break
            return scores
        except Exception as exc:
            logger.warning("Alignment scoring failed: %s — using neutral 0.5 for all", exc)
            return {opp.id: 0.5 for opp in opportunities}

    def _compute_gpa_margin(self, profile: Any, opp: Opportunity) -> float | None:
        """Compute how much the candidate's GPA exceeds any stated minimum."""
        candidate_gpa: float | None = None
        for edu in (profile.education or []):
            if edu.normalized_gpa:
                candidate_gpa = edu.normalized_gpa.value
                break
        if candidate_gpa is None:
            return None
        # Assume a standard minimum of 3.0 on 4.0 scale if not stated
        return candidate_gpa - 3.0

    def _deadline_distance(self, opp: Opportunity) -> int | None:
        """Return days until the nearest deadline, or None."""
        if not opp.deadlines:
            return None
        today = datetime.now(timezone.utc).date()
        for dl in opp.deadlines:
            if dl.date:
                try:
                    from dateutil.parser import parse as parse_date
                    deadline_date = parse_date(dl.date).date()
                    days = (deadline_date - today).days
                    if days >= 0:
                        return days
                except Exception:
                    pass
        return None

    def _build_assessments(
        self,
        opportunities: list[Opportunity],
        rule_results_by_opp: dict,
        scored: list[tuple[str, CompositeScore]],
        ranked: list[tuple],
        alignment_scores: dict[str, float],
    ) -> list[EligibilityAssessment]:
        """Build EligibilityAssessment objects for all opportunities."""
        score_map = {opp_id: score for opp_id, score in scored}
        rank_map = {opp_id: rank for opp_id, _, rank in ranked}

        assessments = []
        for opp in opportunities:
            rule_results = rule_results_by_opp.get(opp.id, [])
            score = score_map.get(opp.id)
            rank = rank_map.get(opp.id)
            gaps = derive_gaps(rule_results)

            eligibility_rules = [
                EligibilityRule(
                    rule_name=r.rule_name,
                    result=r.result,
                    candidate_value=r.candidate_value,
                    required_value=r.required_value,
                    evidence_ref=None,
                )
                for r in rule_results
            ]

            actionable_gaps = [
                ActionableGap(
                    gap_type=g.get("type", "unknown"),
                    description=g.get("description", ""),
                    remedy=g.get("remedy", ""),
                    severity=GapSeverity(g.get("severity", "MEDIUM")),
                    blocking=g.get("blocking", False),
                )
                for g in gaps
            ]

            component_scores = []
            if score:
                for comp_name, comp_val in score.components.items():
                    component_scores.append(ComponentScore(
                        component=comp_name,
                        score=comp_val,
                        weight=0.2,  # simplified — actual weights in scoring.py
                        explanation="",
                    ))

            assessments.append(EligibilityAssessment(
                opportunity_id=opp.id,
                rules=eligibility_rules,
                gaps=actionable_gaps,
                component_scores=component_scores,
                composite_score=score.total if score else 0.0,
                rank=rank,
                confidence=opp.verification.completeness_score if opp.verification else 0.5,
                rationale=f"Score: {score.total:.1f}/100, Rank: {rank}" if score and rank else "",
            ))

        return assessments
