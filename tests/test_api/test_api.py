"""API tests — FastAPI TestClient with fake LLM/search/fetch via a stubbed gateway."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from scholarship_finder.agents.intake import IntakeService
from scholarship_finder.domain.models import (
    ApplicationPackage,
    CandidateProfile,
    ClaimLedgerEntry,
    Education,
    EligibilityAssessment,
    FundingInfo,
    FundingLevel,
    ImprovementPlan,
    Opportunity,
    Preferences,
    SwarmState,
    Suggestion,
    TrustTier,
    VerificationResult,
)
from scholarship_finder.engine.gateway import RunGateway


class _FakeLLM:
    def available(self):
        return True


class _FakeStrategist:
    async def draft(self, state, opp_id, vault=None):
        return ApplicationPackage(
            target_opportunity_id=opp_id, status="ready",
            sop_text="[CANDIDATE_NAME] is a strong fit for this award.",
            claim_ledger=[ClaimLedgerEntry(
                fact_id="F1", source="profile", field_name="degree", value="BSc")],
            removed_claims=["My GPA is 4.9"],
            execution_checklist=["Submit transcript"],
        )


def _final_state(run_state: SwarmState) -> SwarmState:
    """A finished-run state with profile, opp, assessment, plan, SOP."""
    opp = Opportunity(
        id="o1", name="Global Futures Scholarship",
        institution_or_funder="Example Foundation",
        country="Canada", official_url="https://funder.example.org/apply",
        application_url="https://funder.example.org/apply",
        how_to_apply="Online portal",
        funding=FundingInfo(level=FundingLevel.FULL_TUITION_STIPEND,
                            details="tuition + stipend"),
        verification=VerificationResult(
            reachable=True, existence_verified=True, completeness_score=0.75,
            trust_tier=TrustTier.RECOGNIZED_FUNDER),
    )
    asmt = EligibilityAssessment(
        opportunity_id="o1", status="eligible", composite_score=88.5, rank=1,
        rationale="Strong fit", strengths=["high GPA"], concerns=[],
    )
    state = run_state.model_copy(update={
        "profile": CandidateProfile(
            nationality="Indian",
            education=[Education(degree="BSc", field="Nursing")],
            preferences=Preferences(degree_level="masters",
                                    target_fields=["Public Health"],
                                    countries=["Canada"]),
            field_sources={"education": "form"},
            completeness_flags={"has_education": True}),
        "opportunities": [opp],
        "assessments": [asmt],
        "top_5": ["o1"],
        "improvement_plan": ImprovementPlan(
            summary="1 suggestion",
            suggestions=[Suggestion(title="Take GRE", action="Book a GRE slot",
                                    category="test", priority=2)]),
        "applications": {"o1": ApplicationPackage(
            target_opportunity_id="o1", status="ready",
            sop_text="Dear committee, [CANDIDATE_NAME] applies.",
            claim_ledger=[ClaimLedgerEntry(
                fact_id="F1", source="profile", field_name="degree", value="BSc")],
            execution_checklist=["Apply by deadline"])},
    })
    return state


@pytest.fixture()
def gateway():
    gw = RunGateway(graph_runner=None, max_concurrent=None)
    doc_port = MagicMock()
    doc_port.extract = AsyncMock()
    gw.intake_service = IntakeService(document_port=doc_port)
    gw.llm_port = _FakeLLM()
    gw.application_strategist = _FakeStrategist()

    async def runner(state, bus, is_replay=False):
        yield _final_state(state)

    gw.make_graph_runner = lambda **kw: runner
    return gw


@pytest.fixture(autouse=True)
def _no_access_code(monkeypatch):
    """.env may set DEMO_ACCESS_CODE — clear it for most tests."""
    from scholarship_finder.config import get_settings
    monkeypatch.setattr(get_settings(), "demo_access_code", "")


@pytest.fixture()
def client(gateway):
    from scholarship_finder.api.main import app
    app.state.gateway = gateway
    with TestClient(app, raise_server_exceptions=False) as c:
        c.app.state.gateway = gateway  # lifespan rebuilds; re-assert ours
        yield c


def _post_run(client, **over):
    data = {"labels": '["RESUME"]', "profile": "", "display_name": "Jane Doe",
            "github_username": "", "access_code": ""}
    data.update(over.pop("data", {}))
    files = [("files", ("resume.txt", b"Jane Doe, BSc Nursing, GPA 4.1/5.0",
                        "text/plain"))]
    return client.post("/api/runs", files=files, data=data, **over)


def _wait_finished(gateway, run_id, timeout=10.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if gateway.run_finished(run_id):
            return True
        time.sleep(0.05)
    return False


# ── tests ─────────────────────────────────────────────────────────────────────

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "api_key" not in json.dumps(body).lower()


def test_post_run_and_view_model(client, gateway):
    r = _post_run(client)
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    assert _wait_finished(gateway, run_id)

    vm = client.get(f"/api/runs/{run_id}").json()
    assert vm["run_id"] == run_id
    assert vm["top_5"][0]["name"] == "Global Futures Scholarship"
    assert vm["top_5"][0]["assessment"]["composite_score"] == 88.5
    assert vm["improvement_plan"]["suggestions"][0]["title"] == "Take GRE"
    assert vm["applications"]["o1"]["status"] == "ready"
    assert "Jane Doe" in vm["applications"]["o1"]["sop_text"]  # rehydrated
    assert "raw_content" not in json.dumps(vm)
    assert "[REDACTED" not in vm["profile"]["education"]["degree"]


def test_post_validation_error(client):
    # No files and an incomplete form → 400
    r = client.post("/api/runs", data={"profile": "", "labels": ""})
    assert r.status_code == 400
    # Bad profile JSON → 400
    r = client.post(
        "/api/runs",
        files=[("files", ("r.txt", b"x", "text/plain"))],
        data={"profile": "{not json"})
    assert r.status_code == 400


def test_access_code_enforced(client, monkeypatch):
    from scholarship_finder.config import get_settings
    monkeypatch.setattr(get_settings(), "demo_access_code", "sekret")
    r = _post_run(client)
    assert r.status_code == 403
    r = _post_run(client, data={"access_code": "sekret"})
    assert r.status_code == 200


def test_events_stream(client, gateway):
    r = _post_run(client)
    run_id = r.json()["run_id"]
    assert _wait_finished(gateway, run_id)
    with client.stream("GET", f"/api/runs/{run_id}/events?cursor=0") as resp:
        assert resp.status_code == 200
        chunk = "".join(resp.iter_text())
    assert "data:" in chunk and "run_started" in chunk


def test_sop_endpoints(client, gateway):
    r = _post_run(client)
    run_id = r.json()["run_id"]
    assert _wait_finished(gateway, run_id)

    # on-demand draft for the same opportunity → 202
    r = client.post(f"/api/runs/{run_id}/sop", json={"opportunity_id": "o1"})
    assert r.status_code == 202

    st = {}
    for _ in range(40):
        st = client.get(f"/api/runs/{run_id}/sop/o1").json()
        # The freshly drafted package has removed_claims; the pre-seeded
        # package in state does not — poll until the worker overwrites it.
        if st.get("removed_claims"):
            break
        time.sleep(0.1)
    assert st["status"] == "ready"
    assert "Jane Doe" in st["sop_text"]
    assert st["removed_claims"] == ["My GPA is 4.9"]

    dl = client.get(f"/api/runs/{run_id}/sop/o1/download")
    assert dl.status_code == 200
    assert "attachment" in dl.headers.get("content-disposition", "")
    assert "Jane Doe" in dl.text


def test_cancel_and_purge(client, gateway):
    r = _post_run(client)
    run_id = r.json()["run_id"]
    assert _wait_finished(gateway, run_id)
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 200
    assert client.delete(f"/api/runs/{run_id}").status_code == 200
    assert client.get(f"/api/runs/{run_id}").status_code == 404


def test_missing_run_404(client):
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/sop/o1").status_code == 404


def test_sample_endpoint(client):
    r = client.get("/api/samples/rahul_profile.json")
    assert r.status_code == 200 and "nationality" in r.text
    assert client.get("/api/samples/../x").status_code in (400, 404)
    assert client.get("/api/samples/evil.py").status_code == 400


def test_frontend_safety_no_innerhtml():
    js = Path("web/static/js/app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js
    assert "outerHTML" not in js
    assert "insertAdjacentHTML" not in js
