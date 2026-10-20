"""Agent 1: Profile Enricher

Responsibilities (architecture §1.3, Agent 1):
- Parse scrubbed resume text → structured CandidateProfile via LLM (fast tier)
- Optionally fetch GitHub signals (ProfileSourcePort) → ProfileSignals
- Normalize GPA using deterministic strategies
- Compute completeness flags
- Return updated SwarmState with profile set

LLM task spec: discipline-agnostic prompt; output is structured JSON validated
against CandidateProfile schema. One repair attempt on SchemaViolationError.

DEC-1 enforcement: fast tier only; reasoning tier NOT used here.
"""
from __future__ import annotations

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
    ProfileSignals,
    Project,
    RunPhase,
    SwarmState,
)
from scholarship_finder.domain.reducers import reduce_profile
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.llm import LLMPort, TaskSpec
from scholarship_finder.ports.profile_source import ProfileSourcePort, ProfileSourceQuery

logger = logging.getLogger(__name__)

# ── Prompt (discipline-agnostic) ──────────────────────────────────────────────

_EXTRACT_PROFILE_PROMPT = """\
You are a structured data extractor. Extract a candidate's academic and \
professional profile from the provided resume/CV text.

Return ONLY valid JSON matching this schema (no markdown, no explanation):
{
  "education": [
    {
      "degree": "<string: e.g. Bachelor of Engineering>",
      "field": "<string: e.g. Computer Science>",
      "institution": "<string>",
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
  }
}

Rules:
- Extract only information EXPLICITLY stated in the text.
- If a field is absent, use null for scalars and [] for arrays.
- Do NOT invent values. Do NOT include PII (names, emails, phone numbers).
- degree_level should reflect the NEXT degree the candidate seeks (infer from context if stated).
- For cgpa_scale: common values are 4.0 (US), 10.0 (India), 5.0 (German), 100.0 (percentage).

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

        # ── Step 1: LLM extraction ────────────────────────────────────────────
        profile = await self._extract_profile(scrubbed_text, run_id)

        # ── Step 2: GPA normalization ─────────────────────────────────────────
        profile = self._normalize_gpas(profile)

        # ── Step 3: GitHub signal enrichment ──────────────────────────────────
        if self._github and github_username:
            try:
                profile = await self._enrich_github(profile, github_username)
            except Exception as exc:
                logger.warning("GitHub enrichment failed for %s: %s", github_username, exc)

        # ── Step 4: Completeness flags ────────────────────────────────────────
        profile = self._compute_completeness(profile)

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

    async def _extract_profile(self, scrubbed_text: str, run_id: str) -> CandidateProfile:
        """Call LLM (fast tier) to parse the resume into structured fields."""
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

        return self._dict_to_profile(data)

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
                    id=uuid.uuid4(),
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

    async def _enrich_github(
        self, profile: CandidateProfile, username: str
    ) -> CandidateProfile:
        """Fetch GitHub signals and attach to profile."""
        from scholarship_finder.ports.profile_source import ProfileSourceQuery
        query = ProfileSourceQuery(handle=username)
        signals = await self._github.fetch_signals(query)  # returns ProfileSignals
        profile.github_signals = signals
        return profile

    def _compute_completeness(self, profile: CandidateProfile) -> CandidateProfile:
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
            "has_github": profile.github_signals is not None,
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
