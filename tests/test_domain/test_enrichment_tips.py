"""Tests for data-driven enrichment tip generation."""
from __future__ import annotations

import pytest

from scholarship_finder.domain.enrichment_tips import (
    EnrichmentTip,
    format_tips_for_display,
    generate_enrichment_tips,
)
from scholarship_finder.domain.models import (
    CandidateProfile,
    Education,
    LanguageTest,
    NormalizedGPA,
    Preferences,
    Project,
    ProfileSignals,
)


class TestGenerateEnrichmentTips:
    def test_empty_profile_generates_critical_tips(self):
        """Empty profile should generate critical tips for missing data."""
        profile = CandidateProfile()
        tips = generate_enrichment_tips(profile)

        # Should have critical tips for missing essential fields
        assert len(tips) > 0
        critical_tips = [t for t in tips if t.severity == "critical"]
        assert len(critical_tips) > 0

        # Check for specific critical categories
        categories = {t.category for t in critical_tips}
        assert "education" in categories

    def test_complete_profile_generates_fewer_tips(self):
        """Complete profile should generate minimal or no tips."""
        profile = CandidateProfile(
            education=[
                Education(
                    degree="Bachelor of Engineering",
                    field="Computer Science",
                    institution="Test University",
                    cgpa_value=3.5,
                    cgpa_scale=4.0,
                    normalized_gpa=NormalizedGPA(
                        value=3.5,
                        target_scale="us_4point",
                        method="passthrough",
                        confidence=1.0,
                    ),
                )
            ],
            skills=["Python", "Machine Learning", "Data Analysis", "SQL", "Git"],
            projects=[
                Project(
                    id="1",
                    title="ML Project",
                    summary="Built a machine learning model for prediction",
                    technologies=["Python", "scikit-learn", "pandas"],
                ),
                Project(
                    id="2",
                    title="Web App",
                    summary="Developed a web application",
                    technologies=["React", "Node.js", "MongoDB"],
                ),
            ],
            language_tests=[
                LanguageTest(test_name="IELTS", overall_score=7.5),
            ],
            preferences=Preferences(
                countries=["USA", "Germany"],
                degree_level="masters",
                target_fields=["Computer Science", "AI"],
            ),
            github_signals=ProfileSignals(
                projects=[{"name": "repo1", "stars": 10}],
                languages=["Python", "JavaScript"],
                topics=["machine-learning", "web"],
                activity_summary="5 public repos",
            ),
        )

        tips = generate_enrichment_tips(profile)

        # Should have fewer tips for complete profile
        critical_tips = [t for t in tips if t.severity == "critical"]
        high_tips = [t for t in tips if t.severity == "high"]
        assert len(critical_tips) == 0
        assert len(high_tips) == 0

    def test_education_without_gpa_generates_tip(self):
        """Education without GPA should generate a tip."""
        profile = CandidateProfile(
            education=[
                Education(
                    degree="Bachelor of Engineering",
                    field="Computer Science",
                    institution="Test University",
                    cgpa_value=None,
                    cgpa_scale=None,
                )
            ]
        )

        tips = generate_enrichment_tips(profile)
        gpa_tips = [t for t in tips if "GPA" in t.message and t.category == "education"]
        assert len(gpa_tips) > 0
        assert gpa_tips[0].severity == "high"

    def test_low_gpa_generates_tip(self):
        """Low GPA should generate a tip about limited options."""
        profile = CandidateProfile(
            education=[
                Education(
                    degree="Bachelor of Engineering",
                    field="Computer Science",
                    institution="Test University",
                    cgpa_value=2.3,
                    cgpa_scale=4.0,
                    normalized_gpa=NormalizedGPA(
                        value=2.3,
                        target_scale="us_4point",
                        method="passthrough",
                        confidence=1.0,
                    ),
                )
            ]
        )

        tips = generate_enrichment_tips(profile)
        low_gpa_tips = [t for t in tips if "may limit options" in t.message]
        assert len(low_gpa_tips) > 0

    def test_few_skills_generates_tip(self):
        """Few skills should generate a tip to add more."""
        profile = CandidateProfile(
            skills=["Python", "Java"],
        )

        tips = generate_enrichment_tips(profile)
        skill_tips = [t for t in tips if t.category == "skills" and "Add more" in t.message]
        assert len(skill_tips) > 0

    def test_projects_without_technologies_generates_tip(self):
        """Projects without technologies should generate a tip."""
        profile = CandidateProfile(
            projects=[
                Project(
                    id="1",
                    title="ML Project",
                    summary="Built a machine learning model",
                    technologies=[],
                )
            ]
        )

        tips = generate_enrichment_tips(profile)
        tech_tips = [t for t in tips if "technology stack" in t.message.lower()]
        assert len(tech_tips) > 0

    def test_missing_language_test_generates_tip(self):
        """Missing language test should generate a tip."""
        profile = CandidateProfile(
            education=[
                Education(
                    degree="Bachelor of Engineering",
                    field="Computer Science",
                    institution="Test University",
                )
            ],
            skills=["Python"],
        )

        tips = generate_enrichment_tips(profile)
        lang_tips = [t for t in tips if t.category == "language_test"]
        assert len(lang_tips) > 0
        assert "IELTS" in lang_tips[0].message or "TOEFL" in lang_tips[0].message

    def test_low_language_score_generates_tip(self):
        """Low language test score should generate a tip."""
        profile = CandidateProfile(
            language_tests=[
                LanguageTest(test_name="IELTS", overall_score=5.5),
            ]
        )

        tips = generate_enrichment_tips(profile)
        low_score_tips = [t for t in tips if "borderline" in t.message.lower()]
        assert len(low_score_tips) > 0

    def test_missing_preferences_generates_tip(self):
        """Missing preferences should generate tips."""
        profile = CandidateProfile(
            education=[
                Education(
                    degree="Bachelor of Engineering",
                    field="Computer Science",
                    institution="Test University",
                )
            ],
            skills=["Python"],
        )

        tips = generate_enrichment_tips(profile)
        pref_tips = [t for t in tips if t.category == "preferences"]
        assert len(pref_tips) > 0

    def test_missing_github_generates_info_tip(self):
        """Missing GitHub should generate an info tip."""
        profile = CandidateProfile(
            education=[
                Education(
                    degree="Bachelor of Engineering",
                    field="Computer Science",
                    institution="Test University",
                )
            ],
            skills=["Python"],
        )

        tips = generate_enrichment_tips(profile)
        github_tips = [t for t in tips if t.category == "github"]
        assert len(github_tips) > 0
        assert github_tips[0].severity == "info"

    def test_github_with_no_repos_generates_tip(self):
        """GitHub with no repos should generate a tip."""
        profile = CandidateProfile(
            skills=["Python"],
            github_signals=ProfileSignals(
                projects=[],
                languages=[],
                topics=[],
                activity_summary="0 public repos",
            ),
        )

        tips = generate_enrichment_tips(profile)
        repo_tips = [t for t in tips if "no public repos" in t.message.lower()]
        assert len(repo_tips) > 0

    def test_github_username_provided_no_signals_no_tip(self):
        """When GitHub username is provided but signals fail, no tip should be shown."""
        profile = CandidateProfile(
            skills=["Python"],
            github_signals=None,
            completeness_flags={"has_github": True},
        )

        tips = generate_enrichment_tips(profile)
        github_tips = [t for t in tips if t.category == "github"]
        assert len(github_tips) == 0

    def test_education_with_partial_data_no_generic_tip(self):
        """Education with degree and institution but missing field should not trigger generic tip."""
        profile = CandidateProfile(
            education=[
                Education(
                    degree="",  # Missing degree
                    field="Computer Science",
                    institution="State University",
                )
            ],
        )

        tips = generate_enrichment_tips(profile)
        # Should not have the critical "Add education details" tip
        critical_edu_tips = [t for t in tips if t.category == "education" and t.severity == "critical"]
        assert len(critical_edu_tips) == 0

        # Should have the specific "missing degree or institution" tip
        medium_edu_tips = [t for t in tips if t.category == "education" and t.severity == "medium"]
        assert len(medium_edu_tips) > 0
        assert "degree or institution" in medium_edu_tips[0].message.lower()

    def test_education_with_degree_and_institution_no_tip(self):
        """Education with degree and institution should not trigger incomplete tip."""
        profile = CandidateProfile(
            education=[
                Education(
                    degree="Bachelor of Engineering",
                    field="",  # Missing field (but not critical)
                    institution="State University",
                )
            ],
        )

        tips = generate_enrichment_tips(profile)
        edu_tips = [t for t in tips if t.category == "education"]
        # Should not trigger incomplete tip since degree and institution are present
        incomplete_tips = [t for t in edu_tips if "missing degree or institution" in t.message.lower()]
        assert len(incomplete_tips) == 0

    def test_github_username_provided_no_tip(self):
        """When GitHub username is provided (via completeness_flags), no tip should be shown even if signals failed."""
        profile = CandidateProfile(
            skills=["Python"],
            github_signals=None,  # Signals failed to fetch
            completeness_flags={"has_github": True},  # But username was provided
        )

        tips = generate_enrichment_tips(profile)
        github_tips = [t for t in tips if t.category == "github"]
        # Should not show the "Add your GitHub username" tip
        assert len(github_tips) == 0

    def test_tips_sorted_by_priority(self):
        """Tips should be sorted by priority in descending order."""
        profile = CandidateProfile()
        tips = generate_enrichment_tips(profile)

        # Check that priorities are descending
        priorities = [t.priority for t in tips]
        assert priorities == sorted(priorities, reverse=True)


class TestFormatTipsForDisplay:
    def test_formatting_adds_emojis(self):
        """Formatting should add appropriate emojis based on severity."""
        tips = [
            EnrichmentTip(
                category="education",
                severity="critical",
                message="Add education",
                context="Required",
                priority=10,
            ),
            EnrichmentTip(
                category="skills",
                severity="high",
                message="Add skills",
                context="Important",
                priority=8,
            ),
            EnrichmentTip(
                category="projects",
                severity="medium",
                message="Add projects",
                context="Good to have",
                priority=5,
            ),
        ]

        formatted = format_tips_for_display(tips)

        assert len(formatted) == 3
        assert formatted[0].startswith("🚨")
        assert formatted[1].startswith("❌")
        assert formatted[2].startswith("⚠️")

    def test_info_and_low_use_same_emoji(self):
        """Info and low severity should both use the lightbulb emoji."""
        tips = [
            EnrichmentTip(
                category="github",
                severity="info",
                message="Add GitHub",
                context="Optional",
                priority=2,
            ),
            EnrichmentTip(
                category="projects",
                severity="low",
                message="Expand summary",
                context="Nice to have",
                priority=1,
            ),
        ]

        formatted = format_tips_for_display(tips)

        assert all(t.startswith("💡") for t in formatted)

    def test_unknown_severity_uses_default_emoji(self):
        """Unknown severity should default to lightbulb emoji."""
        tips = [
            EnrichmentTip(
                category="test",
                severity="unknown",
                message="Test message",
                context="Test",
                priority=1,
            ),
        ]

        formatted = format_tips_for_display(tips)

        assert formatted[0].startswith("💡")
