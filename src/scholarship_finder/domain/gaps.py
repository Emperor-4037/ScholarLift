"""Deterministic gap aggregation for the Improvement Plan.

Clusters closable UNMET / UNKNOWN eligibility rules across near-miss and top-5
opportunities. ``unlocks`` lists and rationale numbers are computed here —
never invented by the LLM (which only writes title/action text).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scholarship_finder.domain.models import (
    CandidateProfile,
    EligibilityAssessment,
    EligibilityResult,
    Opportunity,
)


@dataclass
class GapCluster:
    """A group of the same closable gap across several opportunities."""
    category: str                    # test | academic | experience | research | documents | profile
    rule_name: str
    required: str                    # strongest requirement seen (e.g. max IELTS)
    requirement_values: list[float] = field(default_factory=list)
    gap_sizes: list[float] = field(default_factory=list)
    unlocks: list[str] = field(default_factory=list)        # opportunity ids
    unlock_names: list[str] = field(default_factory=list)
    deadline_hint: str = ""
    priority: int = 3                 # 1 = do first
    rationale_numbers: str = ""       # deterministic "7.0 vs 6.5" figures
    target: str = ""                  # what to aim for (max requirement)

    def to_seed(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "rule_name": self.rule_name,
            "required": self.required,
            "target": self.target,
            "gap": max(self.gap_sizes) if self.gap_sizes else None,
            "unlocks": self.unlocks,
            "unlock_names": self.unlock_names,
            "deadline_hint": self.deadline_hint,
            "rationale": self.rationale_numbers,
            "priority": self.priority,
        }


_CATEGORY_FOR_RULE = {
    "gpa": "academic",
    "work_experience": "experience",
    "nationality": "profile",
    "degree_level": "academic",
    "field_of_study": "academic",
    "deadline": "documents",
    "funding_fit": "profile",
}


def _rule_category(rule_name: str) -> str:
    if rule_name.startswith("language_"):
        return "test"
    return _CATEGORY_FOR_RULE.get(rule_name, "profile")


def _days(opp: Opportunity) -> int | None:
    dl = opp.next_deadline()
    if dl and dl.iso_date:
        try:
            from datetime import date
            return (date.fromisoformat(dl.iso_date[:10]) - date.today()).days
        except ValueError:
            return None
    return None


def aggregate_gaps(
    assessments: list[EligibilityAssessment],
    opportunities: list[Opportunity],
    profile: CandidateProfile | None = None,
) -> list[GapCluster]:
    """Cluster closable UNMET/UNKNOWN rules across near-miss + top-5 opps.

    Returns clusters sorted by priority (1 = do first), each with the max
    requirement as the target and the opportunities it unlocks.
    """
    opp_map = {o.id: o for o in opportunities}
    # Every assessed verified opp that is not plainly eligible can carry a
    # closable gap — include near_miss, ranked, unverified-requirements and
    # ineligible-by-closable-rule.
    relevant = [
        a for a in assessments
        if a.status in ("near_miss", "requirements_unverified")
        or a.rank is not None
        or (a.status == "ineligible" and any(
            r.result == EligibilityResult.UNMET and r.closable
            for r in a.rules))
    ]
    clusters: dict[tuple[str, str], GapCluster] = {}

    for a in relevant:
        opp = opp_map.get(a.opportunity_id)
        if opp is None:
            continue
        for r in a.rules:
            if r.result == EligibilityResult.MET:
                continue
            # Page-side missing data (destination/host, deadline, funding
            # details) and fuzzy field matches are not student-fixable gaps.
            if r.rule_name in ("destination", "deadline", "funding", "funding_fit",
                               "field_of_study"):
                continue
            if not r.closable:
                continue   # non-closable blockers / unjudged page text are not "gaps you can fix"
            key = (_rule_category(r.rule_name), r.rule_name)
            cl = clusters.get(key)
            if cl is None:
                cl = clusters[key] = GapCluster(
                    category=key[0],
                    rule_name=r.rule_name,
                    required=r.required_value or "",
                )
            if a.opportunity_id not in cl.unlocks:
                cl.unlocks.append(a.opportunity_id)
                cl.unlock_names.append(opp.name or a.opportunity_id)
                days = _days(opp)
                if days is not None and days >= 0 and (
                        not cl.deadline_hint or days < int(cl.deadline_hint.split()[0])):
                    cl.deadline_hint = f"{days} days"
            if r.gap_size is not None:
                cl.gap_sizes.append(r.gap_size)
            cl.required = cl.required or (r.required_value or "")

    for cl in clusters.values():
        # Target = strongest requirement (numeric gaps → max required)
        if cl.gap_sizes:
            cl.rationale_numbers = f"largest gap {max(cl.gap_sizes)}"
        cl.target = cl.required
        n = len(cl.unlocks)
        cl.priority = 1 if n >= 3 else (2 if n == 2 else 3)
        if cl.category == "test":
            cl.priority = min(cl.priority, 2)

    return sorted(clusters.values(), key=lambda c: (c.priority, -len(c.unlocks)))
