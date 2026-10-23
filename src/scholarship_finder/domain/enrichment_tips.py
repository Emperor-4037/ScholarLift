"""Data-driven enrichment tip generation.

Analyzes extracted resume data to provide contextual, actionable recommendations
for profile improvement based on real data rather than static boolean flags.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from scholarship_finder.domain.models import CandidateProfile, Education, NormalizedGPA


@dataclass
class EnrichmentTip:
    """A single actionable enrichment tip with context."""
    category: str  # "education", "skills", "projects", "language_test", "preferences", "github"
    severity: str  # "critical", "high", "medium", "low", "info"
    message: str
    context: str  # Explanation of why this tip is relevant
    priority: int  # Higher = more important (1-10)


def generate_enrichment_tips(profile: CandidateProfile) -> list[EnrichmentTip]:
    """Generate data-driven enrichment tips from a candidate profile.

    Analyzes actual resume data to provide contextual, actionable recommendations
    instead of generic boolean flag messages.

    Args:
        profile: The candidate's profile with extracted resume data.

    Returns:
        List of prioritized enrichment tips.
    """
    tips: list[EnrichmentTip] = []

    # Education analysis
    tips.extend(_analyze_education(profile.education))

    # Skills analysis
    tips.extend(_analyze_skills(profile.skills))

    # Projects analysis
    tips.extend(_analyze_projects(profile.projects))

    # Language test analysis
    tips.extend(_analyze_language_tests(profile.language_tests))

    # Preferences analysis
    tips.extend(_analyze_preferences(profile.preferences))

    # GitHub signals analysis - pass completeness flags for accurate detection
    tips.extend(_analyze_github_signals(profile.github_signals, profile.completeness_flags))

    # Sort by priority (descending)
    tips.sort(key=lambda t: t.priority, reverse=True)

    return tips


def _analyze_education(education: list[Education]) -> list[EnrichmentTip]:
    """Analyze education entries for enrichment opportunities."""
    tips: list[EnrichmentTip] = []

    if not education:
        tips.append(EnrichmentTip(
            category="education",
            severity="critical",
            message="Add your education details (degree, institution, field of study)",
            context="Education is required for scholarship eligibility assessment",
            priority=10,
        ))
        return tips

    # Check for missing GPA
    edu_without_gpa = [e for e in education if e.cgpa_value is None]
    if edu_without_gpa:
        edu_names = [f"{e.degree} at {e.institution}" for e in edu_without_gpa]
        tips.append(EnrichmentTip(
            category="education",
            severity="high",
            message=f"Add GPA/CGPA for: {', '.join(edu_names[:2])}",
            context="GPA is critical for academic fit scoring and many scholarships have minimum requirements",
            priority=8,
        ))

    # Check for missing normalized GPA
    edu_without_normalized = [e for e in education if e.normalized_gpa is None and e.cgpa_value is not None]
    if edu_without_normalized:
        tips.append(EnrichmentTip(
            category="education",
            severity="medium",
            message="GPA normalization will be applied automatically for cross-border comparison",
            context="Your GPA will be converted to standard scales (US 4.0, German 1-4, etc.)",
            priority=5,
        ))

    # Check for low GPA
    edu_with_normalized = [e for e in education if e.normalized_gpa is not None]
    low_gpa_edu = [e for e in edu_with_normalized if e.normalized_gpa.value < 2.5]
    if low_gpa_edu:
        tips.append(EnrichmentTip(
            category="education",
            severity="medium",
            message=f"Your GPA ({low_gpa_edu[0].normalized_gpa.value:.2f} on {low_gpa_edu[0].normalized_gpa.target_scale}) may limit options",
            context="Many competitive scholarships require GPA > 3.0 (US scale). Focus on projects and research experience.",
            priority=6,
        ))

    # Check for incomplete education details (only if education exists but missing critical fields)
    if education:
        missing_critical = [e for e in education if not e.degree or not e.institution or not e.degree.strip() or not e.institution.strip()]
        if missing_critical:
            tips.append(EnrichmentTip(
                category="education",
                severity="medium",
                message="Some education entries missing degree or institution. Verify details are complete.",
                context="Complete education details improve field matching and program recommendations",
                priority=4,
            ))

    return tips


def _analyze_skills(skills: list[str]) -> list[EnrichmentTip]:
    """Analyze skills for enrichment opportunities."""
    tips: list[EnrichmentTip] = []

    if not skills:
        tips.append(EnrichmentTip(
            category="skills",
            severity="high",
            message="Add your technical and domain-specific skills",
            context="Skills are essential for field alignment scoring and matching to research opportunities",
            priority=9,
        ))
        return tips

    skill_count = len(skills)
    if skill_count < 5:
        tips.append(EnrichmentTip(
            category="skills",
            severity="medium",
            message=f"You have {skill_count} skills listed. Add more (target 8-12) for better matching",
            context="More skills improve alignment with diverse research areas and scholarship requirements",
            priority=5,
        ))

    # Check for soft skills vs technical skills
    soft_skill_keywords = ["communication", "leadership", "teamwork", "management", "presentation"]
    technical_count = sum(1 for s in skills if not any(kw in s.lower() for kw in soft_skill_keywords))
    if technical_count < 3:
        tips.append(EnrichmentTip(
            category="skills",
            severity="medium",
            message="Add more technical/domain-specific skills (programming languages, tools, methodologies)",
            context="Technical skills carry more weight in scholarship field alignment assessments",
            priority=6,
        ))

    return tips


def _analyze_projects(projects: list[Any]) -> list[EnrichmentTip]:
    """Analyze projects for enrichment opportunities."""
    tips: list[EnrichmentTip] = []

    if not projects:
        tips.append(EnrichmentTip(
            category="projects",
            severity="high",
            message="Add project descriptions to strengthen your profile",
            context="Projects demonstrate practical experience and are crucial for competitive scholarships",
            priority=8,
        ))
        return tips

    project_count = len(projects)
    if project_count < 2:
        tips.append(EnrichmentTip(
            category="projects",
            severity="medium",
            message=f"You have {project_count} project(s). Add more (target 3-5) to showcase experience",
            context="Multiple projects demonstrate breadth of experience and sustained engagement",
            priority=5,
        ))

    # Check for projects without technologies
    projects_without_tech = [p for p in projects if not p.technologies]
    if projects_without_tech:
        tips.append(EnrichmentTip(
            category="projects",
            severity="medium",
            message=f"{len(projects_without_tech)} project(s) missing technology stack. Add tools/technologies used",
            context="Technology details help match your expertise to research groups and scholarship themes",
            priority=6,
        ))

    # Check for brief summaries
    projects_brief = [p for p in projects if len(p.summary) < 50]
    if projects_brief:
        tips.append(EnrichmentTip(
            category="projects",
            severity="low",
            message=f"Expand project summaries for {len(projects_brief)} project(s) (add impact, outcomes, your role)",
            context="Detailed project descriptions help reviewers understand your contribution and expertise",
            priority=3,
        ))

    return tips


def _analyze_language_tests(language_tests: list[Any]) -> list[EnrichmentTip]:
    """Analyze language test scores for enrichment opportunities."""
    tips: list[EnrichmentTip] = []

    if not language_tests:
        tips.append(EnrichmentTip(
            category="language_test",
            severity="high",
            message="Add IELTS/TOEFL/Duolingo scores if available",
            context="Many international scholarships require English proficiency proof (typically IELTS ≥6.5 or TOEFL ≥90)",
            priority=7,
        ))
        return tips

    # Check for low scores
    test_names = ["ielts", "toefl"]
    low_score_tests = []
    for lt in language_tests:
        if lt.overall_score is not None:
            test_lower = lt.test_name.lower()
            if "ielts" in test_lower and lt.overall_score < 6.5:
                low_score_tests.append(f"{lt.test_name} ({lt.overall_score})")
            elif "toefl" in test_lower and lt.overall_score < 90:
                low_score_tests.append(f"{lt.test_name} ({lt.overall_score})")

    if low_score_tests:
        tips.append(EnrichmentTip(
            category="language_test",
            severity="medium",
            message=f"Language test scores may be borderline: {', '.join(low_score_tests)}",
            context="Some programs require higher scores. Consider retaking or applying to programs with flexible requirements",
            priority=6,
        ))

    return tips


def _analyze_preferences(preferences: Any) -> list[EnrichmentTip]:
    """Analyze preferences for enrichment opportunities."""
    tips: list[EnrichmentTip] = []

    if not preferences.countries and not preferences.target_fields:
        tips.append(EnrichmentTip(
            category="preferences",
            severity="high",
            message="Specify target countries and fields of study",
            context="Preferences enable better scholarship matching and program recommendations",
            priority=8,
        ))
        return tips

    if not preferences.countries:
        tips.append(EnrichmentTip(
            category="preferences",
            severity="medium",
            message="Add target countries for study",
            context="Country-specific scholarships and visa requirements vary significantly",
            priority=6,
        ))

    if not preferences.target_fields:
        tips.append(EnrichmentTip(
            category="preferences",
            severity="medium",
            message="Add target fields of study (e.g., 'Computer Science', 'Biomedical Engineering')",
            context="Field-specific matching improves scholarship relevance",
            priority=6,
        ))

    if not preferences.degree_level:
        tips.append(EnrichmentTip(
            category="preferences",
            severity="medium",
            message="Specify degree level you're seeking (masters, phd, bachelors)",
            context="Degree level affects eligibility and scholarship type",
            priority=5,
        ))

    return tips


def _analyze_github_signals(github_signals: Any, completeness_flags: dict[str, bool] | None = None) -> list[EnrichmentTip]:
    """Analyze GitHub signals for enrichment opportunities."""
    tips: list[EnrichmentTip] = []

    # Check if GitHub username was provided (from completeness flags)
    has_github_provided = completeness_flags.get("has_github", False) if completeness_flags else False

    if github_signals is None and not has_github_provided:
        tips.append(EnrichmentTip(
            category="github",
            severity="info",
            message="Add your GitHub username to showcase your projects and technical activity",
            context="GitHub profile provides verified evidence of coding experience and project contributions",
            priority=4,
        ))
        return tips

    # If GitHub username was provided but signals failed to fetch, don't show the tip
    # The user already provided their GitHub, so no action needed
    if github_signals is None and has_github_provided:
        return tips

    # Analyze GitHub activity if present
    repo_count = len(github_signals.projects)
    if repo_count == 0:
        tips.append(EnrichmentTip(
            category="github",
            severity="info",
            message="GitHub profile found but no public repos. Make relevant projects public for visibility",
            context="Public repositories demonstrate your coding skills and project history",
            priority=3,
        ))
    elif repo_count < 3:
        tips.append(EnrichmentTip(
            category="github",
            severity="low",
            message=f"GitHub has {repo_count} public repo(s). More repos showcase sustained activity",
            context="Consistent contributions across multiple projects strengthen your technical profile",
            priority=2,
        ))

    # Check for language diversity
    if github_signals.languages:
        if len(github_signals.languages) < 2:
            tips.append(EnrichmentTip(
                category="github",
                severity="low",
                message="GitHub shows limited language diversity. Projects in multiple languages demonstrate versatility",
                context="Multi-language proficiency is valued in research and development roles",
                priority=2,
            ))

    return tips


def format_tips_for_display(tips: list[EnrichmentTip]) -> list[str]:
    """Format enrichment tips for UI display with emoji indicators."""
    severity_emoji = {
        "critical": "🚨",
        "high": "❌",
        "medium": "⚠️",
        "low": "💡",
        "info": "💡",
    }

    formatted = []
    for tip in tips:
        emoji = severity_emoji.get(tip.severity, "💡")
        formatted.append(f"{emoji} {tip.message}")

    return formatted
