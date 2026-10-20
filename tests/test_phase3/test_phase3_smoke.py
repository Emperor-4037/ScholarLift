"""End-to-end Phase 3 smoke tests using stub ports.

Tests the full agent pipeline and event bus without any live API calls.
All adapters are stubs; the graph runs to completion deterministically.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from scholarship_finder.domain.models import (
    CandidateProfile,
    DeadlineInfo,
    Education,
    Event,
    EventKind,
    EventSeverity,
    EvidenceRef,
    FieldStatus,
    FundingInfo,
    FundingLevel,
    LLMTier,
    NormalizedGPA,
    Opportunity,
    OpportunityKind,
    Preferences,
    RunBudgets,
    RunDeadlines,
    RunMeta,
    RunOutcome,
    RunPhase,
    SwarmState,
    TrustTier,
    VerificationResult,
)
from scholarship_finder.domain.pii import PIIVault
from scholarship_finder.engine.event_bus import EventBus
from scholarship_finder.engine.gateway import RunGateway, make_initial_state
from scholarship_finder.ports.llm import LLMResult, LLMUsage


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_state(run_id: str | None = None) -> SwarmState:
    return make_initial_state(run_id or str(uuid.uuid4()))


def _make_profile() -> CandidateProfile:
    return CandidateProfile(
        education=[
            Education(
                degree="Bachelor of Technology",
                field="Computer Science",
                institution="IIT Delhi",
                cgpa_value=8.5,
                cgpa_scale=10.0,
                normalized_gpa=NormalizedGPA(
                    value=3.5,
                    target_scale="us_4point",
                    method="indian_to_us",
                    confidence=0.6,
                    note="Approximate",
                ),
            )
        ],
        skills=["Python", "Machine Learning", "PyTorch"],
        projects=[],
        language_tests=[],
        preferences=Preferences(
            countries=["Germany", "Netherlands"],
            degree_level="masters",
            target_fields=["Computer Science", "AI"],
            funding_required=True,
        ),
        completeness_flags={},
        github_signals=None,
    )


def _make_opportunity(opp_id: str | None = None) -> Opportunity:
    return Opportunity(
        id=opp_id or str(uuid.uuid4()),
        kind=OpportunityKind.PROGRAM_WITH_FUNDING,
        name="MSc Computer Science",
        institution_or_funder="TU Berlin",
        country="Germany",
        degree_level="masters",
        official_url="https://tu.berlin/cs-msc",
        fields=[],
        funding=FundingInfo(
            level=FundingLevel.FULL_TUITION_STIPEND,
            details="Full scholarship",
            evidence=None,
            status=FieldStatus.EVIDENCED,
        ),
        deadlines=[
            DeadlineInfo(
                date="2027-01-15",
                description="Winter semester deadline",
                as_of=datetime.now(timezone.utc),
                evidence=None,
                status=FieldStatus.UNVERIFIED,
            )
        ],
        requirements=[],
        verification=VerificationResult(
            reachable=True,
            trust_tier=TrustTier.OFFICIAL_INSTITUTION,
            existence_verified=True,
            completeness_score=0.8,
            failure_notes=[],
        ),
    )


# ── EventBus tests ────────────────────────────────────────────────────────────

class TestEventBus:
    @pytest.mark.asyncio
    async def test_publish_and_poll(self):
        bus = EventBus()
        evt = Event(
            run_id="r1", seq=0,
            timestamp=datetime.now(timezone.utc),
            node="test",
            severity=EventSeverity.INFO,
            kind=EventKind.PROGRESS,
            payload={"x": 1},
        )
        await bus.publish(evt)
        events = bus.since(0)
        assert len(events) == 1
        assert events[0].seq == 1  # stamped with monotonic seq

    @pytest.mark.asyncio
    async def test_cursor_returns_only_new(self):
        bus = EventBus()
        for i in range(5):
            await bus.publish(Event(
                run_id="r1", seq=0,
                timestamp=datetime.now(timezone.utc),
                node="test",
                severity=EventSeverity.INFO,
                kind=EventKind.PROGRESS,
                payload={"i": i},
            ))
        first_three = bus.since(0)[:3]
        cursor = first_three[-1].seq
        new_events = bus.since(cursor)
        assert len(new_events) == 2  # events 4 and 5

    @pytest.mark.asyncio
    async def test_terminal_event_survives_eviction(self):
        bus = EventBus(capacity=3)  # tiny ring
        for i in range(10):
            kind = EventKind.RUN_COMPLETED if i == 5 else EventKind.PROGRESS
            await bus.publish(Event(
                run_id="r1", seq=0,
                timestamp=datetime.now(timezone.utc),
                node="test",
                severity=EventSeverity.INFO,
                kind=kind,
                payload={},
            ))
        # Terminal event must still be accessible from cursor=0
        events = bus.since(0)
        kinds = [e.kind for e in events]
        assert EventKind.RUN_COMPLETED in kinds

    @pytest.mark.asyncio
    async def test_is_terminal_after_complete_event(self):
        bus = EventBus()
        await bus.publish(Event(
            run_id="r1", seq=0,
            timestamp=datetime.now(timezone.utc),
            node="test",
            severity=EventSeverity.INFO,
            kind=EventKind.RUN_COMPLETED,
            payload={},
        ))
        assert bus.is_terminal()

    @pytest.mark.asyncio
    async def test_wait_for_new_returns_immediately_if_events_exist(self):
        bus = EventBus()
        await bus.publish(Event(
            run_id="r1", seq=0,
            timestamp=datetime.now(timezone.utc),
            node="test",
            severity=EventSeverity.INFO,
            kind=EventKind.PROGRESS,
            payload={},
        ))
        events = await bus.wait_for_new(cursor=0, timeout_s=0.1)
        assert len(events) == 1


# ── RunGateway tests ──────────────────────────────────────────────────────────

class TestRunGateway:
    @pytest.mark.asyncio
    async def test_start_run_returns_run_id(self):
        async def dummy_runner(state, bus, **kw):
            return state

        gw = RunGateway(graph_runner=dummy_runner, max_concurrent=3)
        state = _make_state()
        run_id = await gw.start_run(state)
        assert run_id == state.run_meta.run_id

    @pytest.mark.asyncio
    async def test_subscribe_returns_run_started_event(self):
        async def dummy_runner(state, bus, **kw):
            return state

        gw = RunGateway(graph_runner=dummy_runner, max_concurrent=3)
        state = _make_state()
        run_id = await gw.start_run(state)
        await asyncio.sleep(0.05)  # let task fire
        events = gw.subscribe_events(run_id, cursor=0)
        kinds = [e.kind for e in events]
        assert EventKind.RUN_STARTED in kinds

    @pytest.mark.asyncio
    async def test_max_concurrent_runs_enforced(self):
        # graph runner that blocks forever
        async def blocking_runner(state, bus, **kw):
            await asyncio.sleep(60)
            return state

        gw = RunGateway(graph_runner=blocking_runner, max_concurrent=1)
        state1 = _make_state()
        state2 = _make_state()
        await gw.start_run(state1)
        with pytest.raises(RuntimeError, match="Maximum concurrent runs"):
            await gw.start_run(state2)

    @pytest.mark.asyncio
    async def test_cancel_marks_run_finished(self):
        event = asyncio.Event()

        async def waiter(state, bus, **kw):
            await event.wait()
            return state

        gw = RunGateway(graph_runner=waiter, max_concurrent=3)
        state = _make_state()
        run_id = await gw.start_run(state)
        await asyncio.sleep(0.05)

        cancelled = await gw.cancel(run_id)
        assert cancelled is True
        assert gw._runs[run_id].finished

    @pytest.mark.asyncio
    async def test_get_snapshot_returns_state(self):
        async def dummy(state, bus, **kw): return state
        gw = RunGateway(graph_runner=dummy, max_concurrent=3)
        state = _make_state()
        run_id = await gw.start_run(state)
        snap = gw.get_snapshot(run_id)
        assert snap is not None
        assert snap.run_meta.run_id == run_id


# ── Intake Service smoke test ──────────────────────────────────────────────────

class TestIntakeService:
    @pytest.mark.asyncio
    async def test_intake_scrubs_name(self):
        from scholarship_finder.agents.intake import IntakeService
        from scholarship_finder.ports.document import DocumentResult, ExtractedPage

        # Mock document port
        mock_doc_port = MagicMock()
        mock_doc_port.extract = AsyncMock(return_value=DocumentResult(
            pages=[ExtractedPage(page_number=1, text="John Smith is applying with GPA 3.8/4.0", char_count=40)],
            total_pages=1,
            is_scanned=False,
            backend_used="mock",
        ))

        intake = IntakeService(document_port=mock_doc_port, vault_ttl_seconds=900)
        result, vault = await intake.process(
            b"fake pdf bytes",
            "resume.pdf",
            "application/pdf",
            display_name="John Smith",
        )
        assert "[CANDIDATE_NAME]" in result.scrubbed_text
        assert "John Smith" not in result.scrubbed_text
        assert vault.vault_id is not None

    @pytest.mark.asyncio
    async def test_intake_rejects_oversized_file(self):
        from scholarship_finder.agents.intake import IntakeService, _MAX_FILE_BYTES

        mock_doc_port = MagicMock()
        intake = IntakeService(document_port=mock_doc_port)

        with pytest.raises(ValueError, match="too large"):
            await intake.process(
                b"x" * (_MAX_FILE_BYTES + 1),
                "big.pdf",
                "application/pdf",
            )

    @pytest.mark.asyncio
    async def test_intake_rejects_bad_mime(self):
        from scholarship_finder.agents.intake import IntakeService

        mock_doc_port = MagicMock()
        intake = IntakeService(document_port=mock_doc_port)

        with pytest.raises(ValueError, match="Unsupported file type"):
            await intake.process(b"data", "file.exe", "application/octet-stream")


# ── Composition smoke test ────────────────────────────────────────────────────

class TestComposition:
    def test_build_gateway_no_keys(self):
        """Gateway should build successfully even with no API keys (stub mode)."""
        from scholarship_finder.composition import build_gateway
        gw = build_gateway()
        assert isinstance(gw, RunGateway)

    def test_gateway_has_intake_service(self):
        from scholarship_finder.composition import build_gateway
        gw = build_gateway()
        assert hasattr(gw, "intake_service")
