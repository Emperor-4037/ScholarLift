"""Shared pytest fixtures for all tests."""
from __future__ import annotations

import pytest

from scholarship_finder.domain.models import (
    CandidateProfile,
    Education,
    Preferences,
    Project,
    RunDeadlines,
    RunMeta,
    SwarmState,
)
from scholarship_finder.domain.pii import PIIVault


@pytest.fixture()
def sample_vault() -> PIIVault:
    return PIIVault(ttl_seconds=900)


@pytest.fixture()
def sample_profile() -> CandidateProfile:
    return CandidateProfile(
        education=[
            Education(
                degree="B.Tech",
                field="Computer Science",
                institution="IIT Roorkee",
                cgpa_value=8.2,
                cgpa_scale=10.0,
            )
        ],
        skills=["Python", "Machine Learning", "PyTorch"],
        projects=[
            Project(
                title="Sentiment Analyser",
                summary="Trained a BERT model on 50k Amazon reviews achieving 94% accuracy",
                technologies=["Python", "HuggingFace"],
                source="resume",
            )
        ],
        preferences=Preferences(
            countries=["Germany", "Canada", "Singapore"],
            degree_level="masters",
            target_fields=["Artificial Intelligence", "Computer Science"],
            funding_required=True,
        ),
        completeness_flags={
            "has_gpa": True,
            "has_language_test": False,
            "has_github": False,
        },
    )


@pytest.fixture()
def blank_swarm_state() -> SwarmState:
    return SwarmState()
