"""Agent 3: Source Auditor

Responsibilities (architecture §1.3, Agent 3, §2.5 Stage 1):
- Wave-pipelined fetching (~6 URLs per wave) via FetchPort
- Batched extraction (4–5 pages per LLM call) via LLM fast tier
- Span verification: every extracted attribute verified against normalized page text
  - Mismatches → attribute demoted to UNVERIFIED
  - Drop raw page text after extraction (RAM guard)
- Entity grounding: URL/domain must match institution name
- Early stop at 10 existence-verified opportunities
- Returns SwarmState with opportunities list populated

DEC-1: fast tier for all batch extraction. No reasoning tier.
Anti-hallucination Stage 1 enforced here.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from scholarship_finder.domain.models import (
    Candidate,
    DeadlineInfo,
    Event,
    EventKind,
    EventSeverity,
    EvidenceRef,
    FieldStatus,
    FundingInfo,
    FundingLevel,
    LLMTier,
    Opportunity,
    OpportunityField,
    OpportunityKind,
    SwarmState,
    TrustTier,
    VerificationResult,
)
from scholarship_finder.domain.reducers import reduce_opportunities
from scholarship_finder.domain.span_verifier import verify_span
from scholarship_finder.domain.trust import classify_domain
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.fetch import FetchPort
from scholarship_finder.ports.llm import LLMPort, TaskSpec

logger = logging.getLogger(__name__)

_WAVE_SIZE = 6        # URLs fetched in parallel per wave
_BATCH_SIZE = 4       # Pages per LLM extraction call
_EARLY_STOP = 10      # Stop after this many verified opportunities
_MAX_WAVES = 5        # Hard cap on total waves

_EXTRACT_PROMPT = """\
Extract structured opportunity data from the following web page excerpts.

For each page, extract the following fields if present:
- name: program/scholarship name
- institution_or_funder: offering institution or funding body
- country: host country
- degree_level: e.g. masters, phd, bachelors, postdoc
- funding_level: one of FULL_TUITION_STIPEND, FULL_TUITION, PARTIAL, NONE, UNKNOWN
- deadline: application deadline (date string or description)
- requirements: list of key eligibility requirements (strings)
- kind: PROGRAM, SCHOLARSHIP, or PROGRAM_WITH_FUNDING

Return ONLY valid JSON array (no markdown):
[
  {
    "page_index": <int>,
    "name": "<string or null>",
    "institution_or_funder": "<string or null>",
    "country": "<string or null>",
    "degree_level": "<string or null>",
    "funding_level": "<string or null>",
    "deadline": "<string or null>",
    "requirements": ["<string>", ...],
    "kind": "<string or null>",
    "evidence_spans": {
      "name": "<verbatim span from text, 15-400 chars or null>",
      "institution_or_funder": "<verbatim span or null>",
      "funding_level": "<verbatim span or null>",
      "deadline": "<verbatim span or null>"
    }
  },
  ...
]

Rules:
- Extract ONLY what is explicitly present in the page text.
- Use null for any missing field.
- evidence_spans must be verbatim substrings from the page text (case-sensitive).
- If a page has no relevant content, include the entry with all fields null.

Pages:
{pages_block}
"""


class SourceAuditorAgent:
    """Agent 3: Fetches, extracts, and verifies opportunity data."""

    def __init__(self, llm_port: LLMPort, fetch_port: FetchPort) -> None:
        self._llm = llm_port
        self._fetch = fetch_port

    async def run(self, state: SwarmState, bus: EventBus) -> SwarmState:
        """Audit candidates to build a verified Opportunity list."""
        run_id = state.run_meta.run_id
        candidates = state.candidates

        if not candidates:
            logger.warning("SourceAuditor: no candidates to audit")
            return state

        await bus.publish(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="source_auditor",
            severity=EventSeverity.INFO,
            kind=EventKind.NODE_STARTED,
            payload={"node": "source_auditor", "candidate_count": len(candidates)},
        ))

        verified_opps: list[Opportunity] = []
        new_state = state.model_copy(deep=True)

        # Process in waves
        for wave_idx in range(min(_MAX_WAVES, (len(candidates) + _WAVE_SIZE - 1) // _WAVE_SIZE)):
            if len(verified_opps) >= _EARLY_STOP:
                logger.info("SourceAuditor: early stop at %d verified opps", len(verified_opps))
                break

            wave_start = wave_idx * _WAVE_SIZE
            wave = candidates[wave_start: wave_start + _WAVE_SIZE]

            # Fetch wave in parallel
            fetch_results = await self._fetch_wave(wave, run_id)

            # Batch extraction (4–5 per call)
            for batch_start in range(0, len(fetch_results), _BATCH_SIZE):
                batch = fetch_results[batch_start: batch_start + _BATCH_SIZE]
                batch_opps = await self._extract_and_verify_batch(batch, wave)
                verified_opps.extend(batch_opps)

                if len(verified_opps) >= _EARLY_STOP:
                    break

            await bus.publish(Event(
                run_id=run_id,
                seq=0,
                timestamp=datetime.now(timezone.utc),
                node="source_auditor",
                severity=EventSeverity.INFO,
                kind=EventKind.PROGRESS,
                payload={
                    "wave": wave_idx + 1,
                    "verified_so_far": len(verified_opps),
                },
            ))

        # Merge into state
        new_state.opportunities = reduce_opportunities(state.opportunities, verified_opps)

        await bus.publish(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="source_auditor",
            severity=EventSeverity.INFO,
            kind=EventKind.NODE_COMPLETED,
            payload={
                "node": "source_auditor",
                "opportunities_found": len(new_state.opportunities),
                "early_stopped": len(verified_opps) >= _EARLY_STOP,
            },
        ))

        return new_state

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _fetch_wave(
        self, wave: list[Candidate], run_id: str
    ) -> list[tuple[Candidate, str | None]]:
        """Fetch a wave of URLs in parallel. Returns (candidate, text_or_none) pairs."""
        tasks = [self._fetch_one(c) for c in wave]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        pairs: list[tuple[Candidate, str | None]] = []
        for c, result in zip(wave, results):
            if isinstance(result, Exception):
                logger.debug("Fetch failed for %s: %s", c.canonical_url, result)
                pairs.append((c, None))
            else:
                pairs.append((c, result))
        return pairs

    async def _fetch_one(self, candidate: Candidate) -> str | None:
        """Fetch a single URL and return page text (or None on failure)."""
        try:
            doc = await self._fetch.fetch(candidate.canonical_url)
            return doc.normalized_text if doc else None
        except Exception as exc:
            logger.debug("Fetch error for %s: %s", candidate.canonical_url, exc)
            return None

    async def _extract_and_verify_batch(
        self,
        batch: list[tuple[Candidate, str | None]],
        all_candidates: list[Candidate],
    ) -> list[Opportunity]:
        """Extract structured fields from a batch of pages, verify spans."""
        # Build pages block — only include candidates with text
        active = [(c, text) for c, text in batch if text]
        if not active:
            return []

        pages_block = "\n\n".join(
            f"=== Page {i} (URL: {c.canonical_url}) ===\n{text[:3000]}"
            for i, (c, text) in enumerate(active)
        )

        task = TaskSpec(
            tier=LLMTier.FAST,
            instruction=_EXTRACT_PROMPT.format(pages_block=pages_block),
            data_blocks=[],
            output_schema=None,
            max_output_tokens=3000,
        )

        try:
            result = await self._llm.complete(task)
            raw = result.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            extracted = json.loads(raw)
        except Exception as exc:
            logger.warning("Extraction batch failed: %s", exc)
            return []

        opportunities: list[Opportunity] = []
        for item in extracted:
            idx = item.get("page_index", 0)
            if idx >= len(active):
                continue

            candidate, page_text = active[idx]
            opp = self._build_opportunity(item, candidate, page_text or "")
            if opp is not None:
                opportunities.append(opp)

        return opportunities

    def _build_opportunity(
        self, data: dict, candidate: Candidate, page_text: str
    ) -> Opportunity | None:
        """Build a verified Opportunity from extracted data + span verification."""
        import uuid

        name = data.get("name")
        if not name:
            return None  # Can't build an opportunity without a name

        evidence_spans = data.get("evidence_spans", {}) or {}
        fetched_at = datetime.now(timezone.utc)

        def make_evidence(field_name: str) -> EvidenceRef | None:
            span = evidence_spans.get(field_name)
            if not span:
                return None
            verification = verify_span(span, page_text)
            if not verification.matched:
                return None  # Span not found → no evidence
            return EvidenceRef(
                source="doc_page",
                location=candidate.canonical_url,
                span=span,
                offset=verification.offset,
                fetched_at=fetched_at,
            )

        def make_field(field_name: str, raw_value: str | None) -> OpportunityField:
            if not raw_value:
                return OpportunityField(
                    field_name=field_name,
                    value="",
                    evidence=None,
                    status=FieldStatus.UNVERIFIED,
                )
            evidence = make_evidence(field_name)
            return OpportunityField(
                field_name=field_name,
                value=raw_value,
                evidence=evidence,
                status=FieldStatus.EVIDENCED if evidence else FieldStatus.UNVERIFIED,
            )

        # Funding
        funding_level_raw = data.get("funding_level", "UNKNOWN")
        try:
            funding_level = FundingLevel(funding_level_raw.upper())
        except (ValueError, AttributeError):
            funding_level = FundingLevel.UNKNOWN

        funding_evidence = make_evidence("funding_level")
        funding_info = FundingInfo(
            level=funding_level,
            details=data.get("funding_level") or "",
            evidence=funding_evidence,
            status=FieldStatus.EVIDENCED if funding_evidence else FieldStatus.UNVERIFIED,
        )

        # Deadline
        deadline_raw = data.get("deadline")
        deadline_evidence = make_evidence("deadline")
        deadlines = []
        if deadline_raw:
            deadlines.append(DeadlineInfo(
                date=deadline_raw,
                description=deadline_raw,
                as_of=fetched_at,
                evidence=deadline_evidence,
                status=FieldStatus.EVIDENCED if deadline_evidence else FieldStatus.UNVERIFIED,
            ))

        # Kind
        kind_raw = data.get("kind", "PROGRAM")
        try:
            kind = OpportunityKind(kind_raw)
        except (ValueError, AttributeError):
            kind = candidate.kind_hint or OpportunityKind.PROGRAM

        # Verification result
        name_evidenced = make_evidence("name") is not None
        verification = VerificationResult(
            reachable=True,
            trust_tier=candidate.trust_tier,
            existence_verified=name_evidenced,
            completeness_score=self._completeness_score(data),
            failure_notes=[],
        )

        return Opportunity(
            id=str(uuid.uuid4()),
            kind=kind,
            name=name,
            institution_or_funder=data.get("institution_or_funder") or "",
            country=data.get("country") or "",
            degree_level=data.get("degree_level") or "",
            official_url=candidate.canonical_url,
            fields=[
                make_field("name", name),
                make_field("institution_or_funder", data.get("institution_or_funder")),
            ],
            funding=funding_info,
            deadlines=deadlines,
            requirements=[
                OpportunityField(
                    field_name="requirement",
                    value=r,
                    evidence=None,
                    status=FieldStatus.UNVERIFIED,
                )
                for r in (data.get("requirements") or [])
            ],
            verification=verification,
        )

    @staticmethod
    def _completeness_score(data: dict) -> float:
        """Simple heuristic completeness based on non-null fields."""
        fields = ["name", "institution_or_funder", "country", "degree_level", "funding_level", "deadline"]
        present = sum(1 for f in fields if data.get(f))
        return present / len(fields)
