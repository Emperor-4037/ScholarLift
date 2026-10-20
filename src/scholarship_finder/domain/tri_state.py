"""Tri-state eligibility rule evaluation.

Missing data yields UNKNOWN — never a silent failure (architecture §2.5 §4).
Each rule returns MET, UNMET, or UNKNOWN with a candidate and required value.
Rules are composable; the engine evaluates them all deterministically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scholarship_finder.domain.models import EligibilityResult, GapSeverity


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class RuleResult:
    result: EligibilityResult
    rule_name: str
    candidate_value: str | None = None
    required_value: str | None = None
    explanation: str = ""
    blocking: bool = False   # True → caps composite score if UNMET


# ── Individual rules ──────────────────────────────────────────────────────────

def evaluate_gpa_minimum(
    candidate_gpa: float | None,
    required_gpa: float | None,
    rule_name: str = "gpa_minimum",
) -> RuleResult:
    """MET if candidate GPA ≥ required; UNKNOWN if either is missing."""
    if candidate_gpa is None or required_gpa is None:
        return RuleResult(
            result=EligibilityResult.UNKNOWN,
            rule_name=rule_name,
            candidate_value=str(candidate_gpa) if candidate_gpa is not None else None,
            required_value=str(required_gpa) if required_gpa is not None else None,
            explanation="GPA data missing; cannot evaluate",
        )
    met = candidate_gpa >= required_gpa
    margin = round(candidate_gpa - required_gpa, 2)
    return RuleResult(
        result=EligibilityResult.MET if met else EligibilityResult.UNMET,
        rule_name=rule_name,
        candidate_value=str(candidate_gpa),
        required_value=str(required_gpa),
        explanation=f"GPA margin: {margin:+.2f}" if met else f"GPA deficit: {margin:.2f}",
        blocking=not met,
    )


def evaluate_degree_field(
    candidate_fields: list[str] | None,
    required_fields: list[str] | None,
    rule_name: str = "degree_field",
) -> RuleResult:
    """MET if there is any token overlap between candidate and required fields.

    Fuzzy: checks whether any required field token appears in any candidate
    field string (case-insensitive). UNKNOWN if either side is missing.
    """
    if not candidate_fields or not required_fields:
        return RuleResult(
            result=EligibilityResult.UNKNOWN,
            rule_name=rule_name,
            explanation="Degree field data missing; cannot evaluate",
        )

    cand_text = " ".join(candidate_fields).lower()
    for req_field in required_fields:
        for token in req_field.lower().split():
            if len(token) >= 3 and token in cand_text:
                return RuleResult(
                    result=EligibilityResult.MET,
                    rule_name=rule_name,
                    candidate_value=", ".join(candidate_fields),
                    required_value=", ".join(required_fields),
                    explanation=f"Field overlap on '{token}'",
                )

    return RuleResult(
        result=EligibilityResult.UNKNOWN,
        rule_name=rule_name,
        candidate_value=", ".join(candidate_fields),
        required_value=", ".join(required_fields),
        explanation="No obvious field overlap; LLM alignment will judge",
    )


def evaluate_language_test(
    candidate_score: float | None,
    required_score: float | None,
    test_name: str = "language_test",
) -> RuleResult:
    """MET if candidate ≥ required; UNKNOWN if no test data available."""
    rule_name = f"{test_name}_score"
    if candidate_score is None:
        return RuleResult(
            result=EligibilityResult.UNKNOWN,
            rule_name=rule_name,
            required_value=str(required_score) if required_score is not None else None,
            explanation="No language test score on file",
        )
    if required_score is None:
        return RuleResult(
            result=EligibilityResult.UNKNOWN,
            rule_name=rule_name,
            candidate_value=str(candidate_score),
            explanation="Program requirement not stated; cannot evaluate",
        )
    met = candidate_score >= required_score
    return RuleResult(
        result=EligibilityResult.MET if met else EligibilityResult.UNMET,
        rule_name=rule_name,
        candidate_value=str(candidate_score),
        required_value=str(required_score),
        explanation="Score meets requirement" if met else f"Score deficit: {required_score - candidate_score:.1f}",
        blocking=not met,
    )


def evaluate_degree_level(
    candidate_level: str | None,
    required_level: str | None,
    rule_name: str = "degree_level",
) -> RuleResult:
    """MET if candidate's degree level matches the required level."""
    if not candidate_level or not required_level:
        return RuleResult(
            result=EligibilityResult.UNKNOWN,
            rule_name=rule_name,
            explanation="Degree level data missing",
        )
    # Normalise
    c = candidate_level.lower().strip()
    r = required_level.lower().strip()

    # Simple equivalence maps
    masters_aliases = {"masters", "master", "ms", "msc", "ma", "meng", "mtech", "mres"}
    phd_aliases = {"phd", "doctorate", "doctoral", "doctor"}
    bachelors_aliases = {"bachelors", "bachelor", "bs", "bsc", "ba", "beng", "btech", "be"}

    def _group(s: str) -> str:
        if s in masters_aliases:
            return "masters"
        if s in phd_aliases:
            return "phd"
        if s in bachelors_aliases:
            return "bachelors"
        return s

    met = _group(c) == _group(r) or r in ("any", "all")
    return RuleResult(
        result=EligibilityResult.MET if met else EligibilityResult.UNMET,
        rule_name=rule_name,
        candidate_value=candidate_level,
        required_value=required_level,
        explanation="Degree level matches" if met else f"Candidate is {candidate_level}; program requires {required_level}",
        blocking=not met,
    )


def evaluate_citizenship(_required: str | None = None, rule_name: str = "citizenship") -> RuleResult:
    """Always UNKNOWN — we do not collect citizenship data."""
    return RuleResult(
        result=EligibilityResult.UNKNOWN,
        rule_name=rule_name,
        explanation="Citizenship data not collected; candidate must verify independently",
    )


def evaluate_document_availability(rule_name: str = "document_availability") -> RuleResult:
    """Always UNKNOWN — we cannot verify document availability."""
    return RuleResult(
        result=EligibilityResult.UNKNOWN,
        rule_name=rule_name,
        explanation="Document availability not verifiable; listed as an actionable gap",
    )


# ── Batch evaluation & gap derivation ────────────────────────────────────────

def has_blocking_unmet(results: list[RuleResult]) -> bool:
    """True if any rule is both UNMET and blocking."""
    return any(r.result == EligibilityResult.UNMET and r.blocking for r in results)


def derive_gaps(results: list[RuleResult]) -> list[dict[str, Any]]:
    """Convert UNMET / UNKNOWN results into actionable gap dicts."""
    gaps: list[dict[str, Any]] = []
    for r in results:
        if r.result == EligibilityResult.UNMET:
            gaps.append({
                "gap_type": r.rule_name,
                "description": r.explanation,
                "remedy": _remedy_for(r),
                "severity": GapSeverity.CRITICAL.value if r.blocking else GapSeverity.HIGH.value,
                "blocking": r.blocking,
            })
        elif r.result == EligibilityResult.UNKNOWN:
            gaps.append({
                "gap_type": r.rule_name,
                "description": r.explanation,
                "remedy": f"Manually verify '{r.rule_name}' against the official program page",
                "severity": GapSeverity.MEDIUM.value,
                "blocking": False,
            })
    return gaps


def _remedy_for(r: RuleResult) -> str:
    name = r.rule_name
    if "gpa" in name:
        return (
            f"Your GPA ({r.candidate_value}) is below the stated minimum ({r.required_value}). "
            "Consider programs with a lower threshold or strengthen your application with strong recommendations."
        )
    if "language" in name or any(t in name for t in ("ielts", "toefl", "pte")):
        return (
            f"Score {r.required_value} required; you have {r.candidate_value}. "
            "Retake the test or apply to programs that waive English requirements."
        )
    if "degree_level" in name:
        return (
            f"Program requires {r.required_value}; you are applying from {r.candidate_value}. "
            "Confirm entry requirements with the admissions office."
        )
    return f"Meet the '{name}' requirement: required {r.required_value}, candidate has {r.candidate_value}."
