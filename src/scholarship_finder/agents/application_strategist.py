"""Agent 5: Application Strategist

Responsibilities (architecture §1.3, Agent 5, §2.5 Stage 2):
- Build an Allowed-Fact Pack from the top-ranked opportunity + candidate profile
- Generate SOP via LLM (fast tier) — ONLY facts from the allowed pack
- Claim-ledger verification: referential integrity + numeric tokens + proper nouns
- Repair once (fast tier) or strip ungrounded sentences
- Rehydrate [CANDIDATE_NAME] locally from PIIVault (never sent to LLM)
- Generate execution checklist
- Return ApplicationPackage in SwarmState

Anti-hallucination Stage 2 enforced here.
DEC-1: fast tier for SOP generation. No reasoning tier.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from scholarship_finder.domain.claim_verifier import (
    ClaimVerificationResult,
    verify_sop,
)
from scholarship_finder.domain.models import (
    ApplicationPackage,
    ClaimLedgerEntry,
    Event,
    EventKind,
    EventSeverity,
    LLMTier,
    Opportunity,
    SOPSentence,
    SwarmState,
)
from scholarship_finder.domain.pii import PIIVault
from scholarship_finder.domain.reducers import reduce_application
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.llm import LLMPort, TaskSpec

logger = logging.getLogger(__name__)

_SOP_WORD_TARGET = 350   # target word count - reduced for conciseness
_SOP_MAX_WORDS = 450

_SOP_GENERATION_PROMPT = """\
You are a graduate admissions writer. Write a concise Statement of Purpose (SOP) for \
a graduate program application using ONLY the facts provided below.

Requirements:
- Length: {word_target}–{max_words} words (strictly enforced)
- Be concise: avoid repetition, combine related facts into single sentences
- Structure: brief opening motivation → key academic achievements → relevant projects/skills \
→ specific fit with program → brief closing
- DO NOT include any fact citations like [FACT_001] in your output. Write natural prose only.
- Use the facts to inform your writing, but do not reference the fact IDs.
- Use [CANDIDATE_NAME] wherever the applicant's name is needed
- Write in first person, formal academic tone
- Do NOT invent any facts, institutions, scores, or dates not listed below

Allowed Facts:
{facts_block}

Target Program: {program_name} at {institution}

Return ONLY the SOP text with NO fact citations. No preamble, no JSON, no markdown.
"""

_REPAIR_PROMPT = """\
The following SOP sentence has a grounding error. Rewrite it using ONLY the \
allowed facts listed. If it cannot be grounded, reply with exactly: STRIP

Sentence: {sentence}
Error: {error}
Allowed facts: {facts_summary}
"""


class ApplicationStrategistAgent:
    """Agent 5: Generates and verifies a grounded SOP for the top opportunity."""

    def __init__(self, llm_port: LLMPort) -> None:
        self._llm = llm_port

    async def run(
        self,
        state: SwarmState,
        bus: EventBus,
        vault: PIIVault | None = None,
    ) -> SwarmState:
        """Generate a verified ApplicationPackage for the top-ranked opportunity."""
        run_id = state.run_meta.run_id
        profile = state.profile
        top_5 = state.top_5

        if not profile:
            logger.warning("ApplicationStrategist: no profile — skipping")
            return state

        # Pick top opportunity
        top_opp = None
        if top_5 and state.opportunities:
            top_opp_id = top_5[0]
            top_opp = next((o for o in state.opportunities if o.id == top_opp_id), None)

        # If no opportunity found, create a fallback generic opportunity
        if not top_opp:
            logger.warning("ApplicationStrategist: no opportunity found, using fallback")
            from scholarship_finder.domain.models import Opportunity, Funding, Deadline, Requirement
            top_opp = Opportunity(
                id="fallback_opp",
                name="Graduate Program",
                institution_or_funder="Target Institution",
                country="Your Target Country",
                degree_level="Masters/PhD",
                funding=Funding(level="TBD", evidence=""),
                deadlines=[Deadline(date="Check official website", evidence="")],
                requirements=[Requirement(value="Standard graduate program requirements", evidence="")],
                official_url="",
                evidence_spans=[],
            )

        await bus.publish(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="application_strategist",
            severity=EventSeverity.INFO,
            kind=EventKind.NODE_STARTED,
            payload={"node": "application_strategist", "target_opp": top_opp.name},
        ))

        # ── Step 1: Build Allowed-Fact Pack ───────────────────────────────────
        fact_pack, ledger_entries = self._build_fact_pack(profile, top_opp, vault)

        # ── Step 2: Generate SOP ──────────────────────────────────────────────
        raw_sop = await self._generate_sop(fact_pack, top_opp)

        # ── Step 3: Split into sentences + verify ─────────────────────────────
        sentences, verified_results = await self._verify_sop(raw_sop, fact_pack)

        # ── Step 4: Repair or strip ungrounded sentences ─────────────────────
        final_sentences = await self._repair_or_strip(sentences, verified_results, fact_pack)

        # ── Step 5: Local name rehydration ────────────────────────────────────
        sop_text = " ".join(s.text for s in final_sentences)
        if vault and not vault.is_expired:
            sop_text = vault.rehydrate(sop_text)

        # ── Step 5.5: Strip fact citations from final SOP ─────────────────────
        # Remove all [FACT_XXX] citations from the displayed SOP
        # Citations are for internal verification only, not for user display
        sop_text = re.sub(r'\[FACT_\d+\]', '', sop_text)
        sop_text = re.sub(r'\[FACT_\d+(?:,\s*FACT_\d+)*\]', '', sop_text)
        sop_text = re.sub(r'\s+', ' ', sop_text).strip()  # Clean up extra spaces

        word_count = len(sop_text.split())

        # ── Step 6: Build execution checklist ────────────────────────────────
        checklist = self._build_checklist(profile, top_opp, state)

        pkg = ApplicationPackage(
            sop_sentences=final_sentences,
            sop_text=sop_text,
            word_count=word_count,
            claim_ledger=ledger_entries,
            execution_checklist=checklist,
            target_opportunity_id=top_opp_id,
        )

        new_state = state.model_copy(deep=True)
        new_state.application_package = reduce_application(state.application_package, pkg)

        # Emit SOP chunks for streaming UI
        chunk_size = 200  # characters per chunk
        for i in range(0, len(sop_text), chunk_size):
            await bus.publish(Event(
                run_id=run_id,
                seq=0,
                timestamp=datetime.now(timezone.utc),
                node="application_strategist",
                severity=EventSeverity.INFO,
                kind=EventKind.SOP_CHUNK,
                payload={"chunk": sop_text[i: i + chunk_size], "offset": i},
            ))

        await bus.publish(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="application_strategist",
            severity=EventSeverity.INFO,
            kind=EventKind.NODE_COMPLETED,
            payload={
                "node": "application_strategist",
                "word_count": word_count,
                "sentences": len(final_sentences),
                "checklist_items": len(checklist),
            },
        ))

        return new_state

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_fact_pack(
        self, profile: Any, opp: Opportunity, vault: PIIVault | None = None
    ) -> tuple[dict[str, str], list[ClaimLedgerEntry]]:
        """Build a dict of {fact_id: value} and ClaimLedgerEntry list."""
        facts: dict[str, str] = {}
        entries: list[ClaimLedgerEntry] = []
        counter = 1

        def add(field: str, value: str, source: str, evidence_ref=None) -> str:
            nonlocal counter
            fid = f"FACT_{counter:03d}"
            counter += 1
            facts[fid] = value
            entries.append(ClaimLedgerEntry(
                fact_id=fid,
                source=source,  # type: ignore[arg-type]
                field_name=field,
                value=value,
                evidence_ref=evidence_ref,
            ))
            return fid

        # Candidate facts
        # Add candidate name as a fact (LLM will use it with [CANDIDATE_NAME] placeholder)
        if vault and not vault.is_expired:
            candidate_name = vault.retrieve("[CANDIDATE_NAME]")
            if candidate_name:
                add("candidate_name", candidate_name, "profile")

        for edu in (profile.education or []):
            if edu.institution:
                add("institution", edu.institution, "profile")
            if edu.degree:
                add("degree", edu.degree, "profile")
            if edu.field:
                add("field", edu.field, "profile")
            if edu.cgpa_value is not None and edu.cgpa_scale is not None:
                add("gpa", f"{edu.cgpa_value}/{edu.cgpa_scale}", "profile")
            if edu.normalized_gpa:
                add("normalized_gpa", f"{edu.normalized_gpa.value:.2f}/4.0", "profile")

        for lt in (profile.language_tests or []):
            if lt.test_name and lt.overall_score is not None:
                add("language_test", f"{lt.test_name}: {lt.overall_score}", "profile")

        for proj in (profile.projects or [])[:5]:
            if proj.title:
                add("project", proj.title, "profile")

        for skill in (profile.skills or [])[:10]:
            add("skill", skill, "profile")

        # Opportunity facts
        add("program_name", opp.name, "opportunity")
        if opp.institution_or_funder:
            add("program_institution", opp.institution_or_funder, "opportunity")
        if opp.country:
            add("program_country", opp.country, "opportunity")
        if opp.degree_level:
            add("program_level", opp.degree_level, "opportunity")
        if opp.funding:
            add("funding", opp.funding.level.value, "opportunity",
                evidence_ref=opp.funding.evidence)
        for dl in (opp.deadlines or []):
            if dl.date:
                add("deadline", dl.date, "opportunity", evidence_ref=dl.evidence)
        for req in (opp.requirements or [])[:5]:
            add("requirement", req.value, "opportunity")

        logger.info("ApplicationStrategist: built fact pack with %d facts", len(facts))
        if facts:
            sample_facts = list(facts.items())[:3]
            logger.debug("Sample facts: %s", sample_facts)
        else:
            logger.warning("ApplicationStrategist: fact pack is empty!")

        return facts, entries

    def _generate_fallback_sop(self, fact_pack: dict[str, str], opp: Opportunity) -> str:
        """Generate a simple template SOP when LLM fails or returns empty content."""
        # Extract key facts with fallbacks
        institution = fact_pack.get("FACT_001", "the institution")
        degree = fact_pack.get("FACT_002", "graduate program")
        field = fact_pack.get("FACT_003", "your field of study")
        
        # Build a simple template SOP
        sop = f"[CANDIDATE_NAME] is applying for the {degree} in {field} at {institution}. "
        sop += f"With a strong academic background in {field}, [CANDIDATE_NAME] has developed "
        sop += f"skills in various technologies through projects and coursework. "
        sop += f"[CANDIDATE_NAME] is particularly interested in the opportunities at {institution} "
        sop += f"and believes this program aligns well with their career goals."
        
        logger.info("ApplicationStrategist: generated fallback SOP template")
        return sop

    async def _generate_sop(self, fact_pack: dict[str, str], opp: Opportunity) -> str:
        """Generate SOP text using only allowed facts."""
        facts_block = "\n".join(
            f"[{fid}] {value}" for fid, value in fact_pack.items()
        )

        logger.info("ApplicationStrategist: generating SOP with %d facts", len(fact_pack))
        logger.debug("Facts block length: %d chars", len(facts_block))

        task = TaskSpec(
            tier=LLMTier.FAST,
            instruction=_SOP_GENERATION_PROMPT.format(
                word_target=_SOP_WORD_TARGET,
                max_words=_SOP_MAX_WORDS,
                facts_block=facts_block,
                program_name=opp.name,
                institution=opp.institution_or_funder or "the institution",
            ),
            data_blocks=[],
            output_schema=None,
            max_output_tokens=2500,  # Increased from 1500 for better generation
            temperature=0.3,
        )
        
        try:
            result = await self._llm.complete(task)
            raw_sop = result.content.strip()
            logger.info("ApplicationStrategist: LLM returned %d chars", len(raw_sop))
            
            # Validate that the SOP contains actual content, not just placeholders
            if not raw_sop or raw_sop == "[CANDIDATE_NAME]" * 10:
                logger.warning("ApplicationStrategist: LLM returned empty or placeholder-only SOP, using fallback")
                return self._generate_fallback_sop(fact_pack, opp)
            
            return raw_sop
        except Exception as exc:
            logger.error("ApplicationStrategist: LLM call failed: %s, using fallback", exc)
            return self._generate_fallback_sop(fact_pack, opp)

    async def _verify_sop(
        self, raw_sop: str, fact_pack: dict[str, str]
    ) -> tuple[list[SOPSentence], list[ClaimVerificationResult]]:
        """Split SOP into sentences, extract cited fact IDs, verify each."""
        # Split on sentence boundaries
        raw_sentences = re.split(r'(?<=[.!?])\s+', raw_sop.strip())

        logger.info("ApplicationStrategist: verifying SOP with %d sentences", len(raw_sentences))

        # Extract fact citations [FACT_NNN] from each sentence
        sentence_pairs: list[tuple[str, list[str]]] = []
        sop_sentences: list[SOPSentence] = []

        for sent in raw_sentences:
            if not sent.strip():
                continue
            cited = re.findall(r'\[FACT_\d+\]', sent)  # Allow any number of digits
            cited_ids = [c[1:-1] for c in cited]  # strip [ ]
            sentence_pairs.append((sent, cited_ids))
            sop_sentences.append(SOPSentence(text=sent, cited_fact_ids=cited_ids))

        # Verify
        results = verify_sop(sentence_pairs, fact_pack)
        verified_count = sum(1 for r in results if r.passed)
        logger.info("ApplicationStrategist: verification results: %d/%d sentences verified", verified_count, len(results))

        # Mark verified sentences
        for i, res in enumerate(results):
            sop_sentences[i].verified = res.passed

        return sop_sentences, results

    async def _repair_or_strip(
        self,
        sentences: list[SOPSentence],
        results: list[ClaimVerificationResult],
        fact_pack: dict[str, str],
    ) -> list[SOPSentence]:
        """For each failing sentence: try one repair, then strip if still failing."""
        facts_summary = "; ".join(f"{k}={v[:50]}" for k, v in list(fact_pack.items())[:15])
        final: list[SOPSentence] = []

        logger.info("ApplicationStrategist: repairing/striping %d sentences", len(sentences))

        for sent, result in zip(sentences, results):
            if result.passed:
                final.append(sent)
                continue

            # Try one repair
            error_desc = "; ".join(result.failures[:2])
            repair_task = TaskSpec(
                tier=LLMTier.FAST,
                instruction=_REPAIR_PROMPT.format(
                    sentence=sent.text,
                    error=error_desc,
                    facts_summary=facts_summary,
                ),
                data_blocks=[],
                output_schema=None,
                max_output_tokens=200,
            )
            try:
                repair_result = await self._llm.complete(repair_task)
                repaired = repair_result.content.strip()
                if repaired == "STRIP" or not repaired:
                    logger.debug("Stripping ungrounded sentence: %s", sent.text[:60])
                    continue  # strip it
                # Re-verify repaired sentence
                cited = re.findall(r'\[FACT_\d+\]', repaired)  # Allow any number of digits
                cited_ids = [c[1:-1] for c in cited]
                re_results = verify_sop([(repaired, cited_ids)], fact_pack)
                if re_results[0].passed:
                    final.append(SOPSentence(
                        text=repaired, cited_fact_ids=cited_ids, verified=True
                    ))
                else:
                    logger.debug("Repair failed, stripping: %s", sent.text[:60])
            except Exception as exc:
                logger.warning("Repair call failed (%s) — stripping sentence", exc)

        logger.info("ApplicationStrategist: after repair/strip, %d sentences remain", len(final))
        return final

    def _build_checklist(
        self, profile: Any, opp: Opportunity, state: SwarmState
    ) -> list[str]:
        """Generate a deterministic execution checklist from gap data."""
        items: list[str] = [
            f"✅ Apply to: {opp.name} at {opp.institution_or_funder or 'institution'}",
        ]

        # Deadlines
        for dl in (opp.deadlines or []):
            if dl.date:
                items.append(f"📅 Application deadline: {dl.date} — mark calendar now")

        # Funding check
        if opp.funding:
            items.append(
                f"💰 Funding: {opp.funding.level.value} — "
                "confirm current funding status on the official page"
            )

        # Gap-based checklist items
        target_assessment = next(
            (a for a in state.assessments if a.opportunity_id == opp.id), None
        )
        if target_assessment:
            for gap in target_assessment.gaps:
                prefix = "🚨" if gap.blocking else "⚠️"
                items.append(f"{prefix} {gap.remedy}")

        # Standard items always included
        items.extend([
            "📄 Prepare transcripts and GPA certificate (official translation if non-English)",
            "📝 Request recommendation letters at least 6 weeks before deadline",
            "💬 Write supplementary essays if required by the program",
            f"🌐 Official website: {opp.official_url}",
            "✉️ Contact admissions office to confirm current requirements before submitting",
        ])

        return items
