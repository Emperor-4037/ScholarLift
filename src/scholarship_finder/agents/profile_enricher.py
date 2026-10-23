"""Agent 1: Profile Enricher

Responsibilities (architecture §1.3, Agent 1):
|- Parse scrubbed resume text → structured CandidateProfile via LLM (fast tier)
|- Optionally fetch GitHub signals (ProfileSourcePort) → ProfileSignals
|- Normalize GPA using deterministic strategies
|- Compute completeness flags
|- Return updated SwarmState with profile set

LLM task spec: discipline-agnostic prompt; output is structured JSON validated
against CandidateProfile schema. One repair attempt on SchemaViolationError.

DEC-1 enforcement: fast tier only; reasoning tier NOT used here.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from scholarship_finder.domain.gpa import normalize_gpa
from scholarship_finder.domain.models import (
    CandidateProfile,
    Education,
    Event,
    EventKind,
    EventSeverity,
    LanguageTest,
    LLMTier,
    NormalizedGPA,
    Preferences,
    Project,
    SwarmState,
)
from scholarship_finder.domain.reducers import reduce_profile
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.llm import LLMPort, TaskSpec
from scholarship_finder.ports.profile_source import ProfileSourcePort

logger = logging.getLogger(__name__)

# ── Prompt (discipline-agnostic) ──────────────────────────────────────────────

_EXTRACT_PROFILE_PROMPT = """\
You are a structured data extractor. Extract a candidate's academic and \
professional profile from the provided resume/CV text.

Return ONLY valid JSON matching this schema (no markdown, no explanation):
|{
  "education": [
    {
      "degree": "<string: e.g. Bachelor of Engineering, Master of Science>",
      "field": "<string: e.g. Computer Science, Mechanical Engineering>",
      "institution": "<string: university/college name>",
      "cgpa_value": <float or null>,
      "cgpa_scale": <float or null>
    }
  ],
  "skills": ["<string>", ...],
  "projects": [
    {
      "title": "<string>",
      "summary": "<string, 1-2 sentences>",
      "technologies": ["<string>", ...]
    }
  ],
  "language_tests": [
    {
      "test_name": "<string: IELTS/TOEFL/GRE/GMAT/DELF/other>",
      "overall_score": <float or null>
    }
  ],
  "preferences": {
    "countries": ["<string>", ...],
    "degree_level": "<string: masters/phd/bachelors/postdoc>",
    "target_fields": ["<string>", ...],
    "funding_required": <bool>
  },
  "github_username": "<string or null: GitHub username if found in URLs>"
}

Extraction Rules:
1. Extract ONLY information EXPLICITLY stated in the text.
2. If a field is absent, use null for scalars and [] for arrays.
3. Do NOT invent values. Do NOT include PII (names, emails, phone numbers).
4. For education: Look for sections like "Education", "Academic Background", "Qualifications". Extract the degree type, field/major, and institution name. Be thorough - universities may be listed with different headings.
5. For skills: Extract from dedicated "Skills" sections AND from project descriptions. Include technical skills, tools, programming languages, and domain knowledge.
6. For projects: Look for sections like "Projects", "Portfolio", "Work Experience" with project details. Extract project names, brief descriptions, and technologies/tools mentioned.
7. For language tests: Look for IELTS, TOEFL, GRE, GMAT, DELF, PTE, Duolingo scores with numeric values.
8. For preferences: Infer from objective statements, cover letter excerpts, or explicit "Looking for" sections. Countries may be mentioned in location preferences or target universities.
9. For github_username: Look for GitHub URLs (github.com/username) in contact info, project links, or social media sections. Extract the username (the part after github.com/).
10. degree_level should reflect the NEXT degree the candidate seeks (infer from "Objective", "Goal", or stated intentions).
11. For cgpa_scale: Use 4.0 for US/Canada, 10.0 for India, 5.0 for German systems, 100.0 for percentage-based grades.

Resume text:
---
{resume_text}
---
"""


class ProfileEnricherAgent:
    """Agent 1: Parses resume → CandidateProfile, optionally enriches with GitHub."""

    def __init__(
        self,
        llm_port: LLMPort,
        profile_source: ProfileSourcePort | None = None,
    ) -> None:
        self._llm = llm_port
        self._github = profile_source

    async def run(
        self,
        state: SwarmState,
        bus: EventBus,
        scrubbed_text: str,
        github_username: str | None = None,
    ) -> SwarmState:
        """Enrich state with a structured CandidateProfile.

        Parameters
        ----------
        state:
            Current SwarmState (after Intake).
        bus:
            Event bus for progress events.
        scrubbed_text:
            PII-scrubbed resume text from the Intake Service.
        github_username:
            Optional GitHub handle for signal enrichment.

        Returns
        -------
        SwarmState with `profile` set.
        """
        run_id = state.run_meta.run_id

        await bus.publish(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="profile_enricher",
            severity=EventSeverity.INFO,
            kind=EventKind.NODE_STARTED,
            payload={"node": "profile_enricher"},
        ))

        # ── Step 1: Parallel LLM extraction and GitHub enrichment ────────────
        # Run LLM extraction and GitHub fetch concurrently to reduce latency
        logger.debug("ProfileEnricher: starting extraction, github_username input=%s", github_username)
        llm_task = self._extract_profile(scrubbed_text, run_id)
        github_task = None
        if self._github and github_username:
            logger.info("ProfileEnricher: starting GitHub fetch for username: %s", github_username)
            github_task = self._enrich_github_base(github_username)

        # Wait for LLM extraction to complete first (needed for GPA normalization)
        profile, llm_extracted_github = await llm_task
        logger.debug("ProfileEnricher: LLM extraction complete, extracted_github=%s", llm_extracted_github)

        # Use LLM-extracted GitHub username as fallback if manual input not provided
        effective_github_username = github_username or llm_extracted_github
        if llm_extracted_github and not github_username:
            logger.info("ProfileEnricher: using LLM-extracted GitHub username as fallback: %s", llm_extracted_github)

        # If we now have a GitHub username (from LLM) and didn't start GitHub fetch, start it
        if effective_github_username and not github_task and self._github:
            logger.info("ProfileEnricher: starting delayed GitHub fetch for LLM-extracted username: %s", effective_github_username)
            github_task = self._enrich_github_base(effective_github_username)

        # ── Step 2: GPA normalization ─────────────────────────────────────────
        profile = self._normalize_gpas(profile)

        # ── Step 3: Merge GitHub enrichment if concurrent task completed ───────
        if github_task:
            try:
                github_signals = await github_task
                if github_signals is not None:
                    logger.info("ProfileEnricher: GitHub signals fetched successfully for %s", effective_github_username)
                    profile.github_signals = github_signals
                else:
                    logger.warning("ProfileEnricher: GitHub signals fetch returned None for %s", effective_github_username)
            except Exception as exc:
                logger.warning("GitHub enrichment failed for %s: %s", effective_github_username, exc)

        # ── Step 4: Completeness flags ────────────────────────────────────────
        # Pass effective_github_username to compute_completeness to set has_github flag correctly
        profile = self._compute_completeness(profile, github_username=effective_github_username)
        logger.debug("ProfileEnricher: completeness_flags=%s", profile.completeness_flags)

        # ── Step 5: Update state ──────────────────────────────────────────────
        new_state_dict = state.model_dump()
        new_state = SwarmState(**new_state_dict)
        new_state.profile = reduce_profile(state.profile, profile)

        await bus.publish(Event(
            run_id=run_id,
            seq=0,
            timestamp=datetime.now(timezone.utc),
            node="profile_enricher",
            severity=EventSeverity.INFO,
            kind=EventKind.NODE_COMPLETED,
            payload={
                "node": "profile_enricher",
                "education_count": len(profile.education),
                "skills_count": len(profile.skills),
                "projects_count": len(profile.projects),
                "has_gpa": any(e.normalized_gpa for e in profile.education),
                "has_github": profile.github_signals is not None,
            },
        ))

        return new_state

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _extract_profile(self, scrubbed_text: str, run_id: str) -> tuple[CandidateProfile, str | None]:
        """Call LLM (fast tier) to parse the resume into structured fields.

        Returns:
            Tuple of (profile, extracted_github_username)
        """
        task = TaskSpec(
            tier=LLMTier.FAST,
            instruction=_EXTRACT_PROFILE_PROMPT.replace("{resume_text}", scrubbed_text[:12000]),
            data_blocks=[],
            output_schema=None,
            max_output_tokens=2048,
        )

        result = await self._llm.complete(task)
        raw_json = result.content.strip()

        # Strip markdown code fences if present
        if raw_json.startswith("```"):
            raw_json = raw_json.split("```")[1]
            if raw_json.startswith("json"):
                raw_json = raw_json[4:]

        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            logger.warning("Profile extraction: JSON parse error: %s — using empty profile", exc)
            data = {}

        profile = self._dict_to_profile(data)

        # Extract GitHub username from LLM response if available
        extracted_github = data.get("github_username")
        if extracted_github and isinstance(extracted_github, str):
            logger.info("LLM extracted GitHub username: %s", extracted_github)
        else:
            extracted_github = None

        return profile, extracted_github

    def _dict_to_profile(self, data: dict[str, Any]) -> CandidateProfile:
        """Map raw LLM JSON dict → CandidateProfile, tolerating missing/bad fields."""
        education: list[Education] = []
        for edu in data.get("education") or []:
            try:
                education.append(Education(
                    degree=str(edu.get("degree") or ""),
                    field=str(edu.get("field") or ""),
                    institution=str(edu.get("institution") or ""),
                    cgpa_value=_safe_float(edu.get("cgpa_value")),
                    cgpa_scale=_safe_float(edu.get("cgpa_scale")),
                    normalized_gpa=None,
                ))
            except Exception as exc:
                logger.debug("Skipping malformed education entry: %s", exc)

        projects: list[Project] = []
        for proj in data.get("projects") or []:
            try:
                import uuid
                projects.append(Project(
                    id=str(uuid.uuid4()),
                    title=str(proj.get("title") or ""),
                    summary=str(proj.get("summary") or ""),
                    technologies=[str(t) for t in (proj.get("technologies") or [])],
                    source="resume",
                    evidence_ref=None,
                ))
            except Exception as exc:
                logger.debug("Skipping malformed project entry: %s", exc)

        language_tests: list[LanguageTest] = []
        for lt in data.get("language_tests") or []:
            try:
                language_tests.append(LanguageTest(
                    test_name=str(lt.get("test_name") or ""),
                    overall_score=_safe_float(lt.get("overall_score")),
                    component_scores=None,
                    evidence_ref=None,
                ))
            except Exception as exc:
                logger.debug("Skipping malformed language test entry: %s", exc)

        prefs_raw = data.get("preferences") or {}
        preferences = Preferences(
            countries=[str(c) for c in (prefs_raw.get("countries") or [])],
            degree_level=str(prefs_raw.get("degree_level") or "masters"),
            target_fields=[str(f) for f in (prefs_raw.get("target_fields") or [])],
            funding_required=bool(prefs_raw.get("funding_required", True)),
        )

        return CandidateProfile(
            education=education,
            skills=[str(s) for s in (data.get("skills") or [])],
            projects=projects,
            language_tests=language_tests,
            preferences=preferences,
            completeness_flags={},
            github_signals=None,
        )

    def _normalize_gpas(self, profile: CandidateProfile) -> CandidateProfile:
        """Apply GPA normalization strategies in-place."""
        for edu in profile.education:
            if edu.cgpa_value is not None and edu.cgpa_scale is not None:
                try:
                    if edu.cgpa_scale == 4.0:
                        target = "passthrough"
                    elif edu.cgpa_scale == 10.0:
                        target = "indian_to_us"
                    elif edu.cgpa_scale == 100.0:
                        target = "percentage_to_us"
                    else:
                        target = "bavarian"

                    result = normalize_gpa(edu.cgpa_value, edu.cgpa_scale, target=target)
                    edu.normalized_gpa = NormalizedGPA(
                        value=result.value,
                        target_scale=result.target_scale,
                        method=result.method,
                        confidence=result.confidence,
                        note=result.note,
                    )
                except Exception as exc:
                    logger.debug("GPA normalization failed for %s/%s: %s", edu.cgpa_value, edu.cgpa_scale, exc)
        return profile

    async def _enrich_github_base(self, username: str):
        """Fetch GitHub signals (standalone for parallel execution)."""
        if self._github is None:
            logger.debug("GitHub adapter not configured, skipping enrichment")
            return None
        from scholarship_finder.ports.profile_source import ProfileSourceQuery
        query = ProfileSourceQuery(handle=username)
        signals = await self._github.fetch_signals(query)  # returns ProfileSignals
        return signals

    async def _enrich_github(
        self, profile: CandidateProfile, username: str
    ) -> CandidateProfile:
        """Fetch GitHub signals and attach to profile."""
        if self._github is None:
            logger.debug("GitHub adapter not configured, skipping enrichment")
            return profile
        from scholarship_finder.ports.profile_source import ProfileSourceQuery
        query = ProfileSourceQuery(handle=username)
        signals = await self._github.fetch_signals(query)  # returns ProfileSignals
        profile.github_signals = signals
        return profile

    def _compute_completeness(self, profile: CandidateProfile, github_username: str | None = None) -> CandidateProfile:
        """Set completeness_flags for downstream gap analysis."""
        flags = {
            "has_education": bool(profile.education),
            "has_gpa": any(e.cgpa_value is not None for e in profile.education),
            "has_normalized_gpa": any(e.normalized_gpa for e in profile.education),
            "has_skills": bool(profile.skills),
            "has_projects": bool(profile.projects),
            "has_language_test": bool(profile.language_tests),
            "has_preferences": bool(profile.preferences.countries or profile.preferences.target_fields),
            "has_degree_level": bool(profile.preferences.degree_level),
            "has_github": profile.github_signals is not None or github_username is not None,
        }
        profile.completeness_flags = flags
        return profile


# ── Utility ───────────────────────────────────────────────────────────────────

def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
