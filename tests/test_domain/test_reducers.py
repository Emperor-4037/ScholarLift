"""Tests for SwarmState reducer invariants."""
from __future__ import annotations

import pytest

from scholarship_finder.domain.models import (
    Candidate,
    DiagnosticEntry,
    EligibilityAssessment,
    FieldStatus,
    Opportunity,
    OpportunityField,
    RunMeta,
    RunOutcome,
    RunPhase,
    SearchPlan,
    TrustTier,
    VerificationResult,
    CandidateProfile,
    Preferences,
    ApplicationPackage,
    SOPSentence,
)
from scholarship_finder.domain.reducers import (
    reduce_application,
    reduce_assessments,
    reduce_candidates,
    reduce_diagnostics,
    reduce_opportunities,
    reduce_profile,
    reduce_run_meta,
    reduce_search_plans,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def fresh_meta() -> RunMeta:
    return RunMeta()


@pytest.fixture()
def sample_candidate() -> Candidate:
    return Candidate(
        url="https://mit.edu/admissions",
        canonical_url="https://mit.edu/admissions",
        domain="mit.edu",
        trust_tier=TrustTier.OFFICIAL_INSTITUTION,
        source_query_id="q1",
    )


@pytest.fixture()
def sample_opportunity() -> Opportunity:
    return Opportunity(
        id="opp-1",
        name="MIT EECS Masters",
        official_url="https://eecs.mit.edu/masters",
        fields=[
            OpportunityField(
                field_name="min_gpa",
                value="3.0",
                status=FieldStatus.EVIDENCED,
            )
        ],
        verification=VerificationResult(
            reachable=True,
            trust_tier=TrustTier.OFFICIAL_INSTITUTION,
            existence_verified=True,
            completeness_score=0.8,
        ),
    )


# ── RunMeta reducer ───────────────────────────────────────────────────────────

class TestReduceRunMeta:
    def test_phase_advance_allowed(self, fresh_meta):
        result = reduce_run_meta(fresh_meta, {"phase": RunPhase.PHASE_A})
        assert result.phase == RunPhase.PHASE_A

    def test_backward_phase_raises(self, fresh_meta):
        meta_b = reduce_run_meta(fresh_meta, {"phase": RunPhase.PHASE_B})
        with pytest.raises(ValueError, match="Backward phase"):
            reduce_run_meta(meta_b, {"phase": RunPhase.INTAKE})

    def test_outcome_set_once(self, fresh_meta):
        m1 = reduce_run_meta(fresh_meta, {"outcome": RunOutcome.COMPLETE})
        with pytest.raises(ValueError, match="already set"):
            reduce_run_meta(m1, {"outcome": RunOutcome.DEGRADED})

    def test_budget_increment_allowed(self, fresh_meta):
        result = reduce_run_meta(fresh_meta, {"budgets": {"reasoning_calls_used": 2}})
        assert result.budgets.reasoning_calls_used == 2

    def test_budget_decrement_raises(self, fresh_meta):
        m1 = reduce_run_meta(fresh_meta, {"budgets": {"reasoning_calls_used": 2}})
        with pytest.raises(ValueError, match="cannot decrease"):
            reduce_run_meta(m1, {"budgets": {"reasoning_calls_used": 1}})

    def test_degradation_flags_appended(self, fresh_meta):
        m1 = reduce_run_meta(fresh_meta, {"degradation_flags": ["no_github"]})
        m2 = reduce_run_meta(m1, {"degradation_flags": ["no_github", "low_results"]})
        # "no_github" should not be duplicated
        assert m2.degradation_flags.count("no_github") == 1
        assert "low_results" in m2.degradation_flags


# ── Profile reducer ───────────────────────────────────────────────────────────

class TestReduceProfile:
    def test_write_once(self, sample_profile):
        result = reduce_profile(None, sample_profile)
        assert result.skills == sample_profile.skills

    def test_double_write_raises(self, sample_profile):
        p1 = reduce_profile(None, sample_profile)
        with pytest.raises(ValueError, match="already set"):
            reduce_profile(p1, sample_profile)


# ── SearchPlan reducer ────────────────────────────────────────────────────────

class TestReduceSearchPlans:
    def test_append_first_plan(self):
        plan = SearchPlan(round_number=0)
        result = reduce_search_plans([], plan)
        assert len(result) == 1

    def test_append_second_plan(self):
        p0 = SearchPlan(round_number=0)
        p1 = SearchPlan(round_number=1)
        result = reduce_search_plans([p0], p1)
        assert len(result) == 2

    def test_third_plan_raises(self):
        p0 = SearchPlan(round_number=0)
        p1 = SearchPlan(round_number=1)
        p2 = SearchPlan(round_number=2)
        with pytest.raises(ValueError, match="Maximum 2"):
            reduce_search_plans([p0, p1], p2)


# ── Candidate reducer ─────────────────────────────────────────────────────────

class TestReduceCandidates:
    def test_deduplication_by_canonical_url(self, sample_candidate):
        duplicate = sample_candidate.model_copy(update={"trust_tier": TrustTier.AGGREGATOR})
        result = reduce_candidates([sample_candidate], [duplicate])
        # Should keep only one, with higher trust tier
        assert len(result) == 1
        assert result[0].trust_tier == TrustTier.OFFICIAL_INSTITUTION

    def test_new_url_added(self, sample_candidate):
        other = Candidate(
            url="https://stanford.edu/cs",
            canonical_url="https://stanford.edu/cs",
            domain="stanford.edu",
            trust_tier=TrustTier.OFFICIAL_INSTITUTION,
            source_query_id="q2",
        )
        result = reduce_candidates([sample_candidate], [other])
        assert len(result) == 2


# ── Opportunity reducer ───────────────────────────────────────────────────────

class TestReduceOpportunities:
    def test_new_opportunity_added(self, sample_opportunity):
        result = reduce_opportunities([], [sample_opportunity])
        assert len(result) == 1

    def test_upsert_merges_fields(self, sample_opportunity):
        # Create incoming with a new field
        incoming = sample_opportunity.model_copy(deep=True)
        incoming.fields.append(OpportunityField(
            field_name="deadline",
            value="Jan 15, 2025",
            status=FieldStatus.EVIDENCED,
        ))
        result = reduce_opportunities([sample_opportunity], [incoming])
        assert len(result) == 1
        field_names = [f.field_name for f in result[0].fields]
        assert "deadline" in field_names

    def test_evidenced_not_overwritten(self, sample_opportunity):
        # Try to overwrite an EVIDENCED field with UNVERIFIED
        unverified_incoming = sample_opportunity.model_copy(deep=True)
        # Replace the EVIDENCED min_gpa with a worse UNVERIFIED version
        for f in unverified_incoming.fields:
            if f.field_name == "min_gpa":
                f.value = "WRONG"
                f.status = FieldStatus.UNVERIFIED
        result = reduce_opportunities([sample_opportunity], [unverified_incoming])
        min_gpa_fields = [f for f in result[0].fields if f.field_name == "min_gpa"]
        assert min_gpa_fields[0].value == "3.0"  # Original EVIDENCED value kept
        assert min_gpa_fields[0].status == FieldStatus.EVIDENCED


# ── Diagnostics reducer ───────────────────────────────────────────────────────

class TestReduceDiagnostics:
    def test_appends(self):
        entry = DiagnosticEntry(round_number=0, failed_domains=["blocked.edu"])
        result = reduce_diagnostics([], entry)
        assert len(result) == 1
        assert result[0].failed_domains == ["blocked.edu"]

    def test_is_append_only(self):
        e1 = DiagnosticEntry(round_number=0)
        e2 = DiagnosticEntry(round_number=1)
        result = reduce_diagnostics([e1], e2)
        assert len(result) == 2


# ── Assessment reducer ────────────────────────────────────────────────────────

class TestReduceAssessments:
    def test_write_once(self):
        a = EligibilityAssessment(opportunity_id="opp-1")
        assessments, top5 = reduce_assessments([], [a], ["opp-1"])
        assert len(assessments) == 1
        assert top5 == ["opp-1"]

    def test_double_write_raises(self):
        a = EligibilityAssessment(opportunity_id="opp-1")
        with pytest.raises(ValueError, match="already written"):
            reduce_assessments([a], [a], ["opp-1"])


# ── ApplicationPackage reducer ────────────────────────────────────────────────

class TestReduceApplication:
    def _verified_pkg(self) -> ApplicationPackage:
        return ApplicationPackage(
            sop_sentences=[SOPSentence(text="I am a student.", cited_fact_ids=[], verified=True)],
            sop_text="I am a student.",
            target_opportunity_id="opp-1",
        )

    def _unverified_pkg(self) -> ApplicationPackage:
        return ApplicationPackage(
            sop_sentences=[SOPSentence(text="I am a student.", cited_fact_ids=[], verified=False)],
            sop_text="I am a student.",
            target_opportunity_id="opp-1",
        )

    def test_write_when_none(self):
        pkg = self._verified_pkg()
        result = reduce_application(None, pkg)
        assert result.sop_text == "I am a student."

    def test_overwrite_with_verified(self):
        old = self._unverified_pkg()
        # Force write the unverified one first via None
        first = reduce_application(None, old)
        new = self._verified_pkg()
        result = reduce_application(first, new)
        assert all(s.verified for s in result.sop_sentences)

    def test_unverified_overwrite_raises(self):
        old = self._verified_pkg()
        first = reduce_application(None, old)
        with pytest.raises(ValueError, match="unverified sentences"):
            reduce_application(first, self._unverified_pkg())
