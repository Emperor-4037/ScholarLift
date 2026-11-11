"""Agent 1: Profile Enricher — documents + form -> CandidateProfile.

One FAST complete_json over labelled docs (<=9,000 chars total), explicit
field schema. Deterministic merge: form values override extraction;
field_sources recorded. GitHub signals -> projects(source=github) + skills.
Fallback: form-only + regex GPA/test scores. Never crashes on LLM outage.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from scholarship_finder.domain.gpa import normalize_gpa
from scholarship_finder.domain.models import (
    CandidateProfile,
    NormalizedGPA,
    Project,
    SwarmState,
)
from scholarship_finder.engine.context import emit
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.ports.llm import LLMPort, TaskSpec
from scholarship_finder.domain.models import LLMTier
from scholarship_finder.ports.profile_source import ProfileSourcePort, ProfileSourceQuery

logger = logging.getLogger(__name__)

_MAX_DOC_CHARS = 9000
_GPA_RE = re.compile(r"(?:gpa|cgpa|grade|score)[:\s]*([0-9]+(?:\.[0-9]+)?)\s*/\s*([0-9]+(?:\.[0-9]+)?)", re.I)
_TEST_RE = re.compile(r"\b(ielts|toefl|pte|duolingo|gre|gmat)\b[^\d]{0,15}([0-9]{2,3}(?:\.[0-9])?)", re.I)

_SCHEMA = """Extract a student profile as ONE JSON object with keys:
nationality, current_country, languages[], current_level,
education: [{degree, field, institution, level, country, graduation_year,
  cgpa_value, cgpa_scale, grade_system}],
skills[], projects: [{title, summary, technologies[], url}],
research: [{title, kind, venue, year, summary}],
experience: [{role, organization, kind, duration_months, summary}],
activities: [{title, role, summary}],
awards: [{name, year, summary, is_scholarship}],
language_tests: [{test_name, overall_score}],
financial_need: {level, annual_budget_usd, context},
preferences: {countries[], degree_level, target_fields[], funding_required,
  instruction_languages[], intake}
Use only information present in the text; omit unknown keys."""


class ProfileEnricherAgent:
    def __init__(self, llm_port: LLMPort, profile_source: ProfileSourcePort | None = None) -> None:
        self._llm = llm_port
        self._github = profile_source

    async def run(self, state: SwarmState, bus: EventBus,
                  scrubbed_text: str = "", github_username: str | None = None) -> SwarmState:
        intake = state.intake
        docs = intake.documents
        profile = intake.form_profile or CandidateProfile()
        sources: dict[str, str] = dict(profile.field_sources)
        if intake.form_profile is not None:
            for k in profile.model_fields_set:
                sources.setdefault(k, "form")

        # ── LLM extraction over labelled documents ──────────────────────────
        extracted: dict[str, Any] = {}
        doc_text = self._doc_block(docs, scrubbed_text)
        if doc_text.strip():
            try:
                res = await self._llm.complete_json(TaskSpec(
                    tier=LLMTier.FAST, system=_SCHEMA,
                    prompt=f"Documents:\n{doc_text}",
                    max_output_tokens=3000, purpose="profile_extraction",
                ))
                extracted = res.parsed if isinstance(res.parsed, dict) else {}
            except Exception as exc:
                emit("profile_enricher", f"LLM extraction failed ({type(exc).__name__}) — using deterministic fallback",
                     )
                extracted = {}
        if not extracted and doc_text.strip():
            extracted = _regex_extract(doc_text)

        # ── Merge: form overrides extraction ─────────────────────────────────
        try:
            merged = _merge(profile, extracted, sources)
        except Exception as exc:
            emit("profile_enricher", f"Merging extracted details failed ({type(exc).__name__}) — using your form only")
            merged = profile.model_copy(deep=True)

        # ── GitHub signals ───────────────────────────────────────────────────
        gh = github_username or intake.github_username
        if gh and self._github is not None:
            try:
                signals = await self._github.fetch_signals(ProfileSourceQuery(handle=gh))
                if signals:
                    merged.github_signals = signals
                    merged.projects += [Project(
                        title=p.get("name", ""), summary=p.get("description", ""),
                        technologies=[t for t in (p.get("topics") or [])] + ([p["language"]] if p.get("language") else []),
                        url=p.get("url", ""), source="github",
                    ) for p in (signals.projects or [])[:8]]
                    for lang in signals.languages or []:
                        if lang not in merged.skills:
                            merged.skills.append(lang)
                    sources.setdefault("projects", "github")
                    emit("profile_enricher", f"GitHub @{gh}: {len(signals.projects or [])} repos merged")
            except Exception:
                pass

        # ── GPA normalisation + completeness ─────────────────────────────────
        for edu in merged.education:
            if edu.cgpa_value is not None and edu.cgpa_scale and not edu.normalized_gpa:
                try:
                    r = normalize_gpa(edu.cgpa_value, edu.cgpa_scale)
                    edu.normalized_gpa = NormalizedGPA(value=round(r.value, 2), method=r.method)
                except Exception:
                    pass
        merged.field_sources = sources
        merged.completeness_flags = {
            "has_education": bool(merged.education),
            "has_gpa": merged.us_gpa() is not None,
            "has_skills": len(merged.skills) >= 3,
            "has_language_test": bool(merged.language_tests),
            "has_countries": bool(merged.preferences.countries),
            "has_nationality": bool(merged.nationality),
            "has_github": merged.github_signals is not None,
        }
        emit("profile_enricher", _summary(merged))
        return {"profile": merged}

    def _doc_block(self, docs, scrubbed_text) -> str:
        if not docs:
            return (scrubbed_text or "")[:_MAX_DOC_CHARS]
        pri = {"TRANSCRIPT": 0, "RESUME": 1, "OTHER": 2}
        ordered = sorted(docs, key=lambda d: pri.get(d.label, 2))
        out, used = [], 0
        for d in ordered:
            chunk = f"--- {d.label}: {d.filename} ---\n{d.text}"
            if used + len(chunk) > _MAX_DOC_CHARS:
                chunk = chunk[: _MAX_DOC_CHARS - used]
            out.append(chunk); used += len(chunk)
            if used >= _MAX_DOC_CHARS:
                break
        return "\n\n".join(out)


def _merge(base: CandidateProfile, ext: dict[str, Any], sources: dict[str, str]) -> CandidateProfile:
    data = base.model_dump()
    scalar_fields = ("nationality", "current_country", "current_level")
    for f in scalar_fields:
        if not getattr(base, f) and ext.get(f):
            data[f] = ext[f]; sources.setdefault(f, "document")
    for f in ("languages", "skills"):
        extra = [s for s in (ext.get(f) or []) if isinstance(s, str) and s not in data[f]]
        if extra:
            data[f] += extra; sources.setdefault(f, "document")
    list_fields = ("education", "projects", "research", "experience",
                   "activities", "awards", "language_tests")
    for f in list_fields:
        if not data.get(f) and ext.get(f):
            data[f] = ext[f]; sources.setdefault(f, "document")
    if ext.get("financial_need") and base.financial_need.level == "unknown":
        data["financial_need"] = ext["financial_need"]; sources.setdefault("financial_need", "document")
    prefs = ext.get("preferences") or {}
    pdata = data["preferences"]
    for k in ("countries", "target_fields", "instruction_languages"):
        if k != "target_fields" and pdata[k]:
            continue        # the student's own destinations/languages are authoritative
        extra = [c for c in (prefs.get(k) or []) if isinstance(c, str) and c not in pdata[k]]
        pdata[k] += extra
        if extra: sources.setdefault("preferences", "document")
    for k in ("degree_level", "intake"):
        if not pdata.get(k) and prefs.get(k):
            pdata[k] = prefs[k]; sources.setdefault("preferences", "document")
    fr = _as_bool(prefs.get("funding_required"))
    if fr is not None:
        pdata["funding_required"] = fr
    data["preferences"] = pdata
    try:
        return CandidateProfile.model_validate(data)
    except Exception:
        return _salvage(base, data)


def _as_bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return {"true": True, "yes": True, "y": True, "1": True,
                "false": False, "no": False, "n": False, "0": False}.get(v.strip().lower())
    return None


def _valid(data: dict[str, Any]) -> bool:
    try:
        CandidateProfile.model_validate(data)
        return True
    except Exception:
        return False


def _salvage(base: CandidateProfile, data: dict[str, Any]) -> CandidateProfile:
    """One malformed LLM value must never cost the whole profile: keep every
    field / list item / preference that validates on its own, drop the rest."""
    good = base.model_dump()
    for key, val in data.items():
        if _valid({**good, key: val}):
            good[key] = val
        elif isinstance(val, list):
            items: list[Any] = []
            for it in val:
                if _valid({**good, key: items + [it]}):
                    items.append(it)
            good[key] = items
        elif key == "preferences" and isinstance(val, dict):
            prefs = dict(good["preferences"])
            for pk, pv in val.items():
                if _valid({**good, "preferences": {**prefs, pk: pv}}):
                    prefs[pk] = pv
            good["preferences"] = prefs
    return CandidateProfile.model_validate(good)


def _regex_extract(text: str) -> dict[str, Any]:
    ext: dict[str, Any] = {}
    m = _GPA_RE.search(text)
    if m:
        ext["education"] = [{"degree": "", "field": "", "cgpa_value": float(m.group(1)),
                             "cgpa_scale": float(m.group(2))}]
    tests = [{"test_name": t.upper(), "overall_score": float(s)}
             for t, s in _TEST_RE.findall(text)]
    if tests:
        ext["language_tests"] = tests
    return ext


def _summary(p: CandidateProfile) -> str:
    edu = p.primary_education()
    gpa = p.us_gpa()
    parts = [b for b in (edu.degree if edu else "", edu.field if edu else "") if b]
    msg = f"Parsed profile: {', '.join(parts) or 'incomplete'}"
    if edu and edu.cgpa_value:
        msg += f", {edu.cgpa_value}/{edu.cgpa_scale}"
        if gpa:
            msg += f" (≈{gpa:.2f}/4.0)"
    if p.projects:
        msg += f", {len(p.projects)} projects"
    if p.language_tests:
        msg += f", {p.language_tests[0].test_name} {p.language_tests[0].overall_score}"
    if p.preferences.countries:
        msg += f"; targets {', '.join(p.preferences.countries[:4])}"
    return msg
