import pytest
from unittest.mock import AsyncMock, MagicMock
from scholarship_finder.agents.intake import IntakeService
from scholarship_finder.domain.models import CandidateProfile, Education, Preferences
from scholarship_finder.ports.document import DocumentResult


def _svc():
    m = MagicMock()
    m.extract = AsyncMock(return_value=DocumentResult(pages=[], total_pages=1))
    return IntakeService(document_port=m)


async def test_txt_scrubs_name():
    b, v = await _svc().process_documents([(b"John Smith GPA 3.8", "r.txt", "RESUME")], display_name="John Smith")
    assert "[CANDIDATE_NAME]" in b.documents[0].text and "John Smith" not in b.documents[0].text


async def test_form_only_sufficient():
    f = CandidateProfile(education=[Education(degree="BSc", field="Nursing")],
        preferences=Preferences(countries=["UK"], degree_level="masters"))
    b, _ = await _svc().process_documents([], form_profile=f)
    assert b.form_profile is not None


async def test_no_input_rejected():
    with pytest.raises(ValueError):
        await _svc().process_documents([])


async def test_too_many_files_and_bad_ext():
    with pytest.raises(ValueError):
        await _svc().process_documents([(b"x", f"f{i}.txt", "O") for i in range(6)])
    with pytest.raises(ValueError, match="Unsupported"):
        await _svc().process_documents([(b"x", "e.exe", "O")])


async def test_github_from_doc():
    b, _ = await _svc().process_documents([(b"github.com/octocat", "r.txt", "RESUME")])
    assert b.github_username == "octocat"
