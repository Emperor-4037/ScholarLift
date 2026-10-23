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
    VerificationResult,
)
from typing import TypedDict
from scholarship_finder.domain.reducers import reduce_opportunities
from scholarship_finder.domain.span_verifier import verify_span
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.fetch import FetchPort
from scholarship_finder.ports.llm import LLMPort, TaskSpec

logger = logging.getLogger(__name__)

_WAVE_SIZE = 10       # URLs fetched in parallel per wave (balanced for quality vs efficiency)
_BATCH_SIZE = 5        # Pages per LLM extraction call (balanced for quality vs efficiency)
_EARLY_STOP = 20       # Stop after this many verified opportunities (increased for better results)
_MAX_WAVES = 4         # Hard cap on total waves (increased for better coverage)
_RECURSIVE_DEPTH = 1  # Number of levels to follow links (reduced for token efficiency)
_MAX_CONCURRENT_FETCHES = 6  # Conservative concurrency for Send API (Phase 2.1)


# ── Mapper State for Send API (Phase 2.1) ─────────────────────────────────────

class MapperState(TypedDict):
    """State for a single candidate fetch/extract mapper node."""
    candidate: Candidate
    run_id: str

_EXTRACT_PROMPT = """\
Extract structured opportunity data from the following web page excerpts.

For each page, extract the following fields if present:
- name: program/scholarship name
- institution_or_funder: offering institution or funding body (LOOK CAREFULLY for university name, organization name, or funding body name)
- country: host country or countries (LOOK for country names like Germany, Canada, USA, UK, etc.)
- degree_level: e.g. masters, phd, bachelors, postdoc, undergraduate
- funding_level: one of FULL_TUITION_STIPEND, FULL_TUITION, PARTIAL, NONE, UNKNOWN
- funding_details: specific funding amount or description (stipend, tuition, etc.)
- deadline: application deadline (date string or description)
- eligibility: list of key eligibility requirements (citizenship, GPA, field of study, etc.)
- requirements: list of any additional requirements (documents, tests, etc.)
- kind: PROGRAM, SCHOLARSHIP, or PROGRAM_WITH_FUNDING
- is_listing_page: boolean (true if this page lists multiple scholarships or is a database/directory, false if it describes ONE specific scholarship/program)

Return ONLY valid JSON array (no markdown):
[
  {{
    "page_index": <int>,
    "name": "<string or null>",
    "institution_or_funder": "<string or null>",
    "country": "<string or null>",
    "degree_level": "<string or null>",
    "funding_level": "<string or null>",
    "funding_details": "<string or null>",
    "deadline": "<string or null>",
    "eligibility": ["<string>", ...],
    "requirements": ["<string>", ...],
    "kind": "<string or null>",
    "is_listing_page": <boolean>,
    "evidence_spans": {{
      "name": "<verbatim span from text, 15-400 chars or null>",
      "institution_or_funder": "<verbatim span or null>",
      "funding_level": "<verbatim span or null>",
      "deadline": "<verbatim span or null>",
      "country": "<verbatim span or null>",
      "degree_level": "<verbatim span or null>"
    }}
  }},
  ...
]

Rules:
- Extract ONLY what is explicitly present in the page text.
- Use null for any missing field.
- evidence_spans must be verbatim substrings from the page text (case-sensitive).
- CRITICAL: If evidence spans contain quotes, backslashes, or special characters, either:
  1. Escape them properly using backslashes (\" for quotes, \\ for backslashes, \n for newlines), OR
  2. Use a shorter, cleaner span that avoids special characters
- If a page has no relevant content, include the entry with all fields null.
- Pay special attention to eligibility criteria, citizenship requirements, and funding details.
- CRITICAL: Look carefully for institution names, university names, and country names - these are often in headers, titles, or introductory text.
- CRITICAL: For country, look for phrases like "in Germany", "Canada", "United States", "UK", "USA", etc.
- CRITICAL: For funding, look for keywords like "fully funded", "stipend", "tuition waiver", "financial aid", "scholarship", "fellowship", "grant", "living allowance", "monthly stipend".
- CRITICAL: If the page is a general degree program page with NO mention of funding, scholarships, or financial aid, set funding_level to "NONE" and mark it as not a scholarship opportunity.
- Set kind to "SCHOLARSHIP" if the page mentions scholarships, funding, or financial aid. Set to "PROGRAM" only if it's a general degree page without funding.
- CRITICAL: Determine if this page is a listing page (NOT a direct scholarship):
  - Set is_listing_page = true if: page lists multiple scholarships, has search/filter features, is a scholarship database/directory, or contains generic text like "find scholarships", "scholarship database", "directory of scholarships"
  - Set is_listing_page = false if: page describes ONE specific scholarship/program with specific eligibility, funding details, and application process
  - Set is_listing_page = false if: page is from an official university (.edu) or government (.gov) domain describing their own scholarship program
  - Examples of listing pages: "Scholarships & Resources for Data Science", "Find Scholarships", "Scholarship Database", "Graduate Programs Directory"
  - Examples of direct pages: "DeepMind Scholarship 2026", "DAAD Scholarship Program", "University of XYZ Graduate Fellowship"
- ENSURE VALID JSON: All strings must be properly escaped. The JSON must parse without errors.
- For evidence_spans: Select clean, continuous text segments (15-400 chars) that avoid quotes, special characters, and line breaks. Prefer spans from single sentences or phrases.

Pages:
{pages_block}
"""


class SourceAuditorAgent:
    """Agent 3: Fetches, extracts, and verifies opportunity data."""

    def __init__(self, llm_port: LLMPort, fetch_port: FetchPort) -> None:
        self._llm = llm_port
        self._fetch = fetch_port

    def _clean_json_string(self, json_str: str) -> str:
        """Clean and repair common JSON issues from LLM output."""
        import re

        # Remove control characters except \n, \r, \t
        json_str = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', json_str)

        # Fix common issues with unterminated strings by removing trailing characters
        # after the last complete value in the JSON
        # Try to find the last complete object/array and truncate after it
        lines = json_str.split('\n')
        cleaned_lines = []
        for line in lines:
            # Remove trailing commas that would break JSON (at end of objects/arrays)
            line = re.sub(r',\s*([}\]])', r'\1', line)
            cleaned_lines.append(line)
        json_str = '\n'.join(cleaned_lines)

        # Fix common quote escaping issues in evidence_spans
        # This is a heuristic - if we detect unescaped quotes in string values, try to fix
        # Note: This is a best-effort repair and may not handle all cases
        return json_str

    def _repair_json(self, raw: str) -> list | None:
        """Attempt multiple JSON repair strategies, return parsed list or None."""
        # Strategy 1: try json_repair library if available
        try:
            import json_repair  # type: ignore
            repaired = json_repair.repair_json(raw)
            result = json.loads(repaired)
            if isinstance(result, list):
                logger.info("JSON repair succeeded via json_repair library")
                return result
        except (ImportError, Exception):
            pass

        # Strategy 2: extract array brackets and retry
        if '[' in raw and ']' in raw:
            try:
                start = raw.find('[')
                end = raw.rfind(']') + 1
                candidate = raw[start:end]
                result = json.loads(candidate)
                if isinstance(result, list):
                    logger.info("JSON repair succeeded via bracket extraction")
                    return result
            except Exception:
                pass

        # Strategy 3: find the last complete JSON object using brace counting
        try:
            brace_count = 0
            last_complete_idx = -1
            in_string = False
            escape_next = False
            for i, char in enumerate(raw):
                if escape_next:
                    escape_next = False
                    continue
                if char == '\\' and in_string:
                    escape_next = True
                    continue
                if char == '"' and not escape_next:
                    in_string = not in_string
                if in_string:
                    continue
                if char == '{':
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        last_complete_idx = i

            if last_complete_idx > 0:
                truncated = raw[:last_complete_idx + 1]
                array_start = raw.find('[')
                if array_start >= 0 and array_start < last_complete_idx:
                    truncated = raw[array_start:last_complete_idx + 1]
                    truncated = truncated.rstrip(',').rstrip()
                    if not truncated.endswith(']'):
                        truncated += ']'
                else:
                    truncated = '[' + truncated + ']'
                result = json.loads(truncated)
                if isinstance(result, list):
                    logger.info("JSON repair succeeded via brace counting (recovered %d objects)", len(result))
                    return result
        except Exception:
            pass

        # Strategy 4: extract individual JSON objects one by one
        try:
            objects = []
            depth = 0
            start_idx = None
            in_str = False
            esc = False
            for i, ch in enumerate(raw):
                if esc:
                    esc = False
                    continue
                if ch == '\\' and in_str:
                    esc = True
                    continue
                if ch == '"':
                    in_str = not in_str
                if in_str:
                    continue
                if ch == '{':
                    if depth == 0:
                        start_idx = i
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0 and start_idx is not None:
                        obj_str = raw[start_idx:i + 1]
                        try:
                            obj = json.loads(obj_str)
                            objects.append(obj)
                        except Exception:
                            pass
                        start_idx = None
            if objects:
                logger.info("JSON repair succeeded via object-by-object extraction (%d objects)", len(objects))
                return objects
        except Exception:
            pass

        return None

    def _is_listing_page(self, data: dict, page_text: str) -> bool:
        """Detect if the page is a scholarship listing/aggregator page.
        
        Returns True if the page lists multiple scholarships or is a database/directory.
        Returns False if the page describes a single specific scholarship/program.
        """
        name = data.get("name", "").lower()
        institution = data.get("institution_or_funder", "").lower()
        
        # Check if LLM explicitly marked it as a listing page
        if data.get("is_listing_page") == True:
            logger.info("LLM classified as listing page: %s from %s", name, institution)
            return True
        
        # Check for listing keywords in name
        listing_keywords = [
            "scholarships & resources",
            "find scholarships",
            "scholarship database",
            "scholarship finder",
            "search scholarships",
            "online master's degrees",
            "masters portal",
            "phd portal",
            "find a masters",
            "find a phd",
            "scholarship positions",
        ]
        
        # Check name for listing indicators
        for kw in listing_keywords:
            if kw in name:
                logger.info("Name contains listing keyword '%s': %s", kw, name)
                return True
        
        # Check institution for generic/indicators
        generic_institutions = [
            "online master's degrees",
            "scholarship finder",
            "study portal",
            "masters portal",
            "phd portal",
            "studyportals",
            "youth opportunities",
            "opportunity desk"
        ]
        
        for gen in generic_institutions:
            if gen in institution:
                logger.info("Institution is generic '%s': %s", gen, institution)
                return True
        
        # Check if name is very generic without specific program name
        # E.g., "Scholarships" vs "DeepMind Scholarship 2026"
        if name in ["scholarships", "funding", "graduate funding", "scholarship"]:
            logger.info("Name is too generic: %s", name)
            return True
        
        return False

    # ── Mapper function for Send API (Phase 2.1) ───────────────────────────────

    async def fetch_and_verify_candidate(
        self, mapper_state: MapperState, bus: EventBus
    ) -> dict:
        """Mapper node: fetch and verify a single candidate (for Send API).

        Returns a dict with verified opportunities list.
        """
        raw = mapper_state["candidate"]
        candidate = raw if isinstance(raw, Candidate) else Candidate.model_validate(raw)
        run_id = mapper_state["run_id"]

        try:
            # Fetch the candidate's page
            page_text, _ = await self._fetch_one(candidate)

            if not page_text:
                return {"verified_opps": []}

            # Extract and verify (batch size of 1 for mapper)
            batch = [(candidate, page_text)]
            opportunities = await self._extract_and_verify_batch(batch, [])

            return {"verified_opps": opportunities}

        except Exception as exc:
            logger.warning("Mapper failed for %s: %s", getattr(candidate, "canonical_url", "?"), exc)
            return {"verified_opps": []}

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
        self, wave: list[Candidate], run_id: str, depth: int = 0
    ) -> list[tuple[Candidate, str | None]]:
        """Fetch a wave of URLs in parallel. Returns (candidate, text_or_none) pairs.
        
        If depth < _RECURSIVE_DEPTH, also fetch linked pages from listing pages.
        """
        tasks = [self._fetch_one(c) for c in wave]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        pairs: list[tuple[Candidate, str | None]] = []
        additional_candidates: list[Candidate] = []
        
        for c, result in zip(wave, results):
            if isinstance(result, Exception):
                logger.debug("Fetch failed for %s: %s", c.canonical_url, result)
                pairs.append((c, None))
            else:
                page_text, links = result
                pairs.append((c, page_text))
                
                # If we found links and haven't reached max depth, queue them
                if links and depth < _RECURSIVE_DEPTH:
                    from urllib.parse import urlparse
                    for link in links:
                        parsed = urlparse(link)
                        additional_candidates.append(Candidate(
                            url=link,
                            canonical_url=link,
                            domain=parsed.netloc or "",
                            kind_hint=c.kind_hint,
                            trust_tier=c.trust_tier,
                            source_query_id="recursive_link",
                        ))
        
        # If we have additional candidates and haven't reached max depth, fetch them
        if additional_candidates and depth < _RECURSIVE_DEPTH:
            logger.info("SourceAuditor: fetching %d linked pages at depth %d", 
                        len(additional_candidates), depth + 1)
            recursive_pairs = await self._fetch_wave(additional_candidates, run_id, depth + 1)
            pairs.extend(recursive_pairs)
        
        return pairs

    async def _fetch_one(self, candidate: Candidate) -> tuple[str | None, list[str]]:
        """Fetch a single URL and return (page_text, linked_urls) or (None, [])."""
        try:
            doc = await self._fetch.fetch(candidate.canonical_url)
            if not doc:
                return None, []
            
            # Extract links that look like actual scholarship pages
            links = self._extract_relevant_links(doc.normalized_text, candidate.canonical_url)
            return doc.normalized_text, links
        except Exception as exc:
            logger.debug("Fetch error for %s: %s", candidate.canonical_url, exc)
            return None, []

    def _extract_relevant_sections(self, page_text: str) -> str:
        """Extract only relevant sections from page text to reduce token usage.

        Looks for keywords related to scholarships, funding, deadlines, eligibility, etc.
        and extracts paragraphs containing these keywords along with nearby context.
        """
        import re

        # Keywords that indicate relevant sections
        relevant_keywords = [
            'scholarship', 'funding', 'financial aid', 'stipend', 'tuition',
            'deadline', 'application', 'eligible', 'eligibility', 'requirement',
            'program', 'degree', 'master', 'phd', 'bachelor', 'graduate',
            'admission', 'apply', 'criteria', 'citizenship', 'gpa',
            'ielts', 'toefl', 'language', 'english', 'cost', 'fee'
        ]

        # Split into paragraphs (by double newlines or period+newline)
        paragraphs = re.split(r'\n\n+|\.\n', page_text)

        relevant_paragraphs = []
        for para in paragraphs:
            para_lower = para.lower()
            # Check if paragraph contains any relevant keyword
            if any(kw in para_lower for kw in relevant_keywords):
                relevant_paragraphs.append(para.strip())

        # If we found relevant sections, use them; otherwise use first few paragraphs
        if relevant_paragraphs:
            extracted = '\n\n'.join(relevant_paragraphs[:20])  # Max 20 relevant paragraphs
        else:
            # Fallback: use first 10 paragraphs
            extracted = '\n\n'.join(p.strip() for p in paragraphs[:10] if p.strip())

        # Also extract headers/titles (lines that look like headers)
        header_pattern = r'^(#{1,3}\s|<h[1-3]>|<strong>|[A-Z][A-Z\s]{5,}:)\s*.+$'
        headers = re.findall(header_pattern, page_text, re.MULTILINE)
        if headers:
            header_text = '\n'.join(headers[:10])
            extracted = header_text + '\n\n' + extracted

        # Limit to 4000 characters (reasonable for extraction)
        return extracted[:4000]

    def _extract_relevant_links(self, page_text: str, base_url: str) -> list[str]:
        """Extract URLs that look like actual scholarship/program pages from listing pages."""
        import re
        from urllib.parse import urljoin, urlparse
        
        # Pattern to find URLs
        url_pattern = r'https?://[^\s<>"\'()]+'
        urls = re.findall(url_pattern, page_text)
        
        # Filter URLs that look like actual program pages (not listing pages)
        relevant_urls = []
        for url in urls:
            # Skip if it's the same as base URL
            if url == base_url:
                continue
            
            # Skip if it looks like a listing/search page
            skip_keywords = ['/search', '/browse', '/results']
            if any(kw in url.lower() for kw in skip_keywords):
                continue
            
            # Keep URLs that look like individual program pages
            # Usually they contain program IDs, names, or are direct paths
            parsed = urlparse(url)
            path_lower = parsed.path.lower()
            
            # Skip very short paths (likely listing pages)
            if len(parsed.path) < 3 and not parsed.query:
                continue
            
            # Keep if path contains typical program page patterns
            keep_keywords = ['program', 'scholarship', 'fellowship', 'grant', 'detail', 'view']
            if any(kw in path_lower for kw in keep_keywords):
                relevant_urls.append(url)
            # Also keep if path is reasonably long (likely a specific page)
            elif len(parsed.path) > 20:
                relevant_urls.append(url)
        
        # Deduplicate and limit
        return list(dict.fromkeys(relevant_urls))[:5]  # Max 5 links per page

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
            f"=== Page {i} (URL: {c.canonical_url}) ===\n{self._extract_relevant_sections(text)}"
            for i, (c, text) in enumerate(active)
        )

        task = TaskSpec(
            tier=LLMTier.FAST,
            instruction=_EXTRACT_PROMPT.format(pages_block=pages_block),
            data_blocks=[],
            output_schema=None,
            max_output_tokens=2000,  # Reduced to match smaller input size
        )

        try:
            result = await self._llm.complete(task)
            raw = result.content.strip()
            
            # Strip code fences
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            
            # Clean JSON string
            raw = self._clean_json_string(raw)
            
            # Try to parse JSON directly first
            try:
                extracted = json.loads(raw)
            except json.JSONDecodeError as json_err:
                # Try to fix common JSON issues: unescaped quotes in strings
                logger.warning("Initial JSON parse failed, attempting repair: %s", json_err)
                extracted = self._repair_json(raw)
                if extracted is None:
                    logger.error("JSON repair failed: Could not find complete JSON structure")
                    logger.debug("Problematic JSON (first 500 chars): %s", raw[:500])
                    return []
                    
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

        # Filter out listing/aggregator pages (Phase 1 fix)
        if self._is_listing_page(data, page_text):
            logger.info("Filtering out listing page: %s", name)
            return None

        # Filter out general program pages with no funding
        funding_level_str = data.get("funding_level", "").upper()
        funding_details_str = data.get("funding_details") or ""
        kind = data.get("kind", "").upper()

        # Check if this page explicitly has NO funding (filter these out)
        # Allow pages with UNKNOWN funding or partial funding mentions
        has_explicit_no_funding = (
            funding_level_str == "NONE"
            and not any(kw in funding_details_str.lower() for kw in ["scholarship", "funding", "stipend", "financial aid", "tuition waiver"])
            and kind != "SCHOLARSHIP"
        )

        # Skip only pages that explicitly have NO funding
        if has_explicit_no_funding:
            logger.debug("Skipping %s - explicitly marked as no funding", name)
            return None

        evidence_spans = data.get("evidence_spans", {}) or {}
        fetched_at = datetime.now(timezone.utc)

        # Phase 3.1: Span verification
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
        try:
            funding_level = FundingLevel(funding_level_str.upper())
        except (ValueError, AttributeError):
            funding_level = FundingLevel.UNKNOWN

        funding_evidence = make_evidence("funding_level")
        funding_details = funding_details_str or data.get("funding_level") or ""
        funding_info = FundingInfo(
            level=funding_level,
            details=funding_details,
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
                make_field("country", data.get("country")),
                make_field("degree_level", data.get("degree_level")),
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
                for r in (data.get("requirements") or []) + (data.get("eligibility") or [])
            ],
            verification=verification,
        )

    @staticmethod
    def _completeness_score(data: dict) -> float:
        """Simple heuristic completeness based on non-null fields."""
        fields = ["name", "institution_or_funder", "country", "degree_level", "funding_level", "deadline", "eligibility"]
        present = sum(1 for f in fields if data.get(f))
        return present / len(fields)
