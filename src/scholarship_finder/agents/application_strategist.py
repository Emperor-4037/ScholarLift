"""Agent 5: Application Strategist — grounded, verified SOP per opportunity.

Fact pack F1..Fn from profile + opportunity → ONE reasoning call (550–750
words, [F#] after every factual sentence, [CANDIDATE_NAME] placeholder) →
verify_sop_f → ONE batched repair call → re-verify → remaining failures are
dropped into removed_claims. Display text = citations stripped; the real
name is rehydrated locally from the vault (never sent to the LLM).
Failure → status="failed", never a crash.
"""
from __future__ import annotations

import logging
import re

from scholarship_finder.domain import claim_verifier as cv
from scholarship_finder.domain.models import (
    ApplicationPackage,
    ClaimLedgerEntry,
    LLMTier,
    SOPSentence,
    SwarmState,
)
from scholarship_finder.engine.context import emit
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.llm import LLMPort, TaskSpec

logger = logging.getLogger(__name__)

_SENT = re.compile(r"(?<=[.!?])\s+")

def _ctx():
    try:
        from scholarship_finder.engine import context as rc
        return rc.current()
    except Exception:
        return None


_PROMPT = """Write a statement of purpose of {lo}-{hi} words for:
{opp}
STRICT RULES: every sentence containing a fact must end with citations like
[F1][F3]; use ONLY the facts below; refer to the student as [CANDIDATE_NAME];
no invented numbers, names or achievements; return plain text only.

FACTS:
{facts}"""

_REPAIR = """These sentences failed grounding checks. Return a JSON array
[{"index": i, "text": "rewritten sentence citing [F#] or null to drop"}]
using ONLY the same facts.
Failures:
{items}
FACTS:
{facts}"""


class ApplicationStrategistAgent:
    def __init__(self, llm_port: LLMPort) -> None:
        self._llm = llm_port

    async def run(self, state: SwarmState, bus: EventBus, vault=None) -> dict:
        if not state.top_5:
            emit("application_strategist", "No top-5 — no SOP to draft")
            return {}
        pkg = await self._draft_pkg(state, state.top_5[0])
        emit("application_strategist",
             f"SOP {pkg.status}: {pkg.word_count} words, "
             f"{len(pkg.removed_claims)} claims removed")
        return {"applications": {**state.applications, state.top_5[0]: pkg}}

    async def draft(self, state: SwarmState, opp_id: str, vault=None) -> ApplicationPackage:
        """On-demand SOP for one opportunity (used by gateway.request_sop)."""
        return await self._draft_pkg(state, opp_id)

    # ── internals ────────────────────────────────────────────────────────────

    async def _draft_pkg(self, state: SwarmState, opp_id: str) -> ApplicationPackage:
        opp = state.opportunity(opp_id)
        if state.profile is None or opp is None:
            return ApplicationPackage(
                target_opportunity_id=opp_id, status="failed",
                error="profile or opportunity missing")
        facts, ledger = _fact_pack(state, opp)
        try:
            res = await self._llm.complete(TaskSpec(
                tier=LLMTier.REASONING,
                system=_PROMPT.format(
                    lo=600, hi=700, opp=_opp_brief(opp),
                    facts="\n".join(f"[{k}] {v}" for k, v in facts.items())),
                max_output_tokens=1800, purpose="sop_generation"))
            raw = (res.content or "").strip()
        except Exception as exc:
            emit("application_strategist",
                 f"SOP generation failed ({type(exc).__name__})")
            return ApplicationPackage(
                target_opportunity_id=opp_id, status="failed", error=str(exc)[:200])

        allowed = frozenset({opp.name, opp.institution_or_funder})
        sentences = [s.strip() for s in _SENT.split(raw) if s.strip()]
        results = cv.verify_sop_f(sentences, facts, extra_allowed=allowed)
        failing = [i for i, r in enumerate(results) if not r.passed]

        if failing:
            sentences, results = await self._repair(
                sentences, results, failing, facts, allowed)

        kept, removed = [], []
        for s, r in zip(sentences, results):
            if r is None:
                removed.append(s)
                continue
            if r.passed:
                kept.append((s, r))
            else:
                removed.append(cv.strip_citations(s) or s)
        display = [cv.strip_citations(s) for s, _ in kept]
        text = " ".join(display)
        sop_sentences = [
            SOPSentence(text=t, cited_fact_ids=cv.parse_cited_ids(s),
                        verified=True, repaired=results[i].repaired if False else False)
            for i, ((s, _), t) in enumerate(zip(kept, display))
        ]
        return ApplicationPackage(
            target_opportunity_id=opp_id,
            status="ready" if kept else "failed",
            sop_sentences=sop_sentences,
            sop_text=text,
            claim_ledger=ledger,
            removed_claims=removed,
            execution_checklist=_checklist(opp),
            error="" if kept else "all sentences failed verification",
        )

    async def _repair(self, sentences, results, failing, facts, allowed):
        try:
            items = "\n".join(
                f"{i}: {sentences[i]}  (errors: {'; '.join(results[i].failures)})"
                for i in failing)
            res = await self._llm.complete_json(TaskSpec(
                tier=LLMTier.FAST,
                system=_REPAIR.format(
                    items=items,
                    facts="\n".join(f"[{k}] {v}" for k, v in facts.items())),
                max_output_tokens=1500, purpose="sop_repair"))
            parsed = res.parsed or []
            new_s, new_r = list(sentences), list(results)
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                i = item.get("index")
                if not isinstance(i, int) or i >= len(sentences):
                    continue
                if item.get("text") is None:
                    new_r[i] = None
                    continue
                new_s[i] = item["text"]
                new_r[i] = cv.verify_sentence_f(new_s[i], facts, extra_allowed=allowed)
                new_r[i].sentence_index = i
            return new_s, new_r
        except Exception:
            return sentences, results


# ── helpers ───────────────────────────────────────────────────────────────────

def _fact_pack(state, opp) -> tuple[dict[str, str], list[ClaimLedgerEntry]]:
    p = state.profile
    facts: dict[str, str] = {}
    ledger: list[ClaimLedgerEntry] = []
    n = 0

    def add(field_name: str, value: str, source="profile"):
        nonlocal n
        if not value:
            return
        n += 1
        fid = f"F{n}"
        facts[fid] = value
        ledger.append(ClaimLedgerEntry(fact_id=fid, source=source,
                                       field_name=field_name, value=value))

    edu = p.primary_education()
    if edu:
        add("degree", f"{edu.degree} in {edu.field} at {edu.institution}".strip(" in at"))
        if edu.cgpa_value:
            gpa = p.us_gpa()
            add("gpa", f"GPA {edu.cgpa_value}/{edu.cgpa_scale}" +
                (f" (≈{gpa:.2f}/4.0)" if gpa else ""))
    if p.nationality:
        add("nationality", p.nationality)
    for t in p.language_tests[:3]:
        add("language_test", f"{t.test_name} {t.overall_score}")
    for pr in p.projects[:3]:
        add("project", f"{pr.title}: {pr.summary}")
    for r in p.research[:2]:
        add("research", f"{r.title} ({r.venue or r.kind})")
    for e in p.experience[:2]:
        add("experience", f"{e.role} at {e.organization}"
            + (f" ({e.duration_months} months)" if e.duration_months else ""))
    for a in p.awards[:2]:
        add("award", a.name)
    if p.preferences.target_fields:
        add("goals", f"wants {p.preferences.degree_level or 'further study'} in "
              + ", ".join(p.preferences.target_fields[:3]))
    add("opp_name", opp.name, "opportunity")
    add("opp_provider", opp.institution_or_funder, "opportunity")
    if opp.country:
        add("opp_country", opp.country, "opportunity")
    if opp.funding and (opp.funding.details or opp.funding.level):
        add("opp_funding", f"{opp.funding.level.value} {opp.funding.details}",
            "opportunity")
    for sc in (opp.selection_criteria or [])[:3]:
        add("opp_selection", sc, "opportunity")
    for cr in (opp.eligibility.other or [])[:3]:
        add("opp_requirement", cr, "opportunity")
    return facts, ledger


def _opp_brief(opp) -> str:
    return (f"{opp.name} at {opp.institution_or_funder} "
            f"({opp.country or ', '.join(opp.host_countries)})")


def _checklist(opp) -> list[str]:
    out = []
    if opp.how_to_apply:
        out.append(f"Apply: {opp.how_to_apply}")
    if opp.application_url:
        out.append(f"Portal: {opp.application_url}")
    dl = opp.next_deadline()
    if dl and (dl.iso_date or dl.date):
        out.append(f"Deadline: {dl.iso_date or dl.date}")
    for lr in opp.eligibility.language_requirements:
        out.append(f"Provide {lr.test} ≥ {lr.min_score}")
    out.append("Prepare transcripts, references and this statement of purpose")
    return out
