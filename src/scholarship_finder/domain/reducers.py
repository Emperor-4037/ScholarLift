"""Safe reducer functions for SwarmState slice updates.

Enforces the merge semantics from architecture §2.4:
  - run_meta:  monotonic budget decrements, single outcome transition
  - profile:   single-write (raise on double-write)
  - search_plans: append-by-round, max 2 rounds
  - candidates:   keyed set union by canonical_url, keep highest trust_tier
  - opportunities: keyed upsert with EVIDENCED-field promotion
  - diagnostics:   append-only
  - assessments & top_5: single-write replace
  - application_package: staged replace

All reducers are PURE functions: they take immutable input and return new objects.
"""
from __future__ import annotations

from scholarship_finder.domain.models import (
    ApplicationPackage,
    Candidate,
    DiagnosticEntry,
    EligibilityAssessment,
    FieldStatus,
    Opportunity,
    RunMeta,
    RunOutcome,
    RunPhase,
    SearchPlan,
    TrustTier,
    CandidateProfile,
)


# ── TrustTier ordering (for merge — higher = more trusted) ───────────────────

_TRUST_ORDER = {
    TrustTier.OFFICIAL_INSTITUTION: 3,
    TrustTier.RECOGNIZED_FUNDER: 2,
    TrustTier.AGGREGATOR: 1,
    TrustTier.UNKNOWN: 0,
}


def _higher_trust(a: TrustTier, b: TrustTier) -> TrustTier:
    return a if _TRUST_ORDER[a] >= _TRUST_ORDER[b] else b


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Reducers
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def reduce_run_meta(current: RunMeta, delta: dict) -> RunMeta:
    """Apply a delta dict to RunMeta, enforcing invariants.

    Allowed mutations:
      - phase: must advance forward (INTAKE → PHASE_A → PHASE_B → COMPLETED)
      - outcome: may only be set once from IN_PROGRESS to a terminal state
      - budgets.reasoning_calls_used: may only increase
      - budgets.fast_calls_used: may only increase
      - budgets.search_credits_used: may only increase
      - budgets.fetch_count: may only increase
      - retry_count: may only increase
      - degradation_flags: append-only

    Raises:
        ValueError: On backward phase transitions or double outcome set.
    """
    data = current.model_dump()

    phase_order = [RunPhase.INTAKE, RunPhase.PHASE_A, RunPhase.PHASE_B, RunPhase.COMPLETED]

    # Phase transition
    if "phase" in delta:
        new_phase = RunPhase(delta["phase"])
        curr_idx = phase_order.index(current.phase)
        new_idx = phase_order.index(new_phase)
        if new_idx < curr_idx:
            raise ValueError(
                f"Backward phase transition: {current.phase} → {new_phase}"
            )
        data["phase"] = new_phase

    # Outcome: set once
    if "outcome" in delta:
        new_outcome = RunOutcome(delta["outcome"])
        if current.outcome != RunOutcome.IN_PROGRESS:
            raise ValueError(
                f"outcome already set to {current.outcome}; cannot set to {new_outcome}"
            )
        data["outcome"] = new_outcome

    # Budget increments (monotonic)
    if "budgets" in delta:
        b_delta = delta["budgets"]
        b = data["budgets"]
        for key in ("reasoning_calls_used", "fast_calls_used", "search_credits_used", "fetch_count"):
            if key in b_delta:
                new_val = b_delta[key]
                if new_val < b[key]:
                    raise ValueError(f"Budget counter {key} cannot decrease: {b[key]} → {new_val}")
                b[key] = new_val

    # retry_count monotonic
    if "retry_count" in delta:
        if delta["retry_count"] < current.retry_count:
            raise ValueError("retry_count cannot decrease")
        data["retry_count"] = delta["retry_count"]

    # degradation_flags append-only
    if "degradation_flags" in delta:
        existing = set(current.degradation_flags)
        new_flags = [f for f in delta["degradation_flags"] if f not in existing]
        data["degradation_flags"] = current.degradation_flags + new_flags

    return RunMeta.model_validate(data)


def reduce_profile(
    current: CandidateProfile | None,
    new_profile: CandidateProfile,
) -> CandidateProfile:
    """Write the candidate profile once; raise if already set."""
    if current is not None:
        raise ValueError("CandidateProfile is already set; single-write invariant violated")
    return new_profile


def reduce_search_plans(
    current: list[SearchPlan],
    new_plan: SearchPlan,
) -> list[SearchPlan]:
    """Append a new search plan; max 2 rounds."""
    if len(current) >= 2:
        raise ValueError(
            f"Maximum 2 search plan rounds allowed; already have {len(current)}"
        )
    return current + [new_plan]


def reduce_candidates(
    current: list[Candidate],
    new_candidates: list[Candidate],
) -> list[Candidate]:
    """Keyed set union by canonical_url; keep the highest trust_tier on collision."""
    merged: dict[str, Candidate] = {c.canonical_url: c for c in current}

    for nc in new_candidates:
        if nc.canonical_url in merged:
            existing = merged[nc.canonical_url]
            # Retain the higher-trust entry
            if _TRUST_ORDER[nc.trust_tier] > _TRUST_ORDER[existing.trust_tier]:
                merged[nc.canonical_url] = nc
        else:
            merged[nc.canonical_url] = nc

    return list(merged.values())


def reduce_opportunities(
    current: list[Opportunity],
    new_opps: list[Opportunity],
) -> list[Opportunity]:
    """Keyed upsert by opportunity id; EVIDENCED fields are never demoted.

    For each field in the incoming opportunity:
      - If the current record has that field as EVIDENCED, skip the incoming value.
      - Otherwise, update.

    Additionally, deduplicates by official_url to prevent duplicate opportunities
    with different IDs from the same URL.
    """
    # First, deduplicate by official_url to prevent same-URL duplicates
    url_deduped: dict[str, Opportunity] = {}

    # Process current opportunities
    for opp in current:
        url = opp.official_url
        if url and url not in url_deduped:
            url_deduped[url] = opp
        elif url:
            # Keep the one with higher completeness_score
            existing = url_deduped[url]
            if opp.verification.completeness_score > existing.verification.completeness_score:
                url_deduped[url] = opp

    # Process new opportunities
    for opp in new_opps:
        url = opp.official_url
        if url and url not in url_deduped:
            url_deduped[url] = opp
        elif url:
            # Keep the one with higher completeness_score
            existing = url_deduped[url]
            if opp.verification.completeness_score > existing.verification.completeness_score:
                url_deduped[url] = opp

    # Now merge by ID for field promotion (EVIDENCED protection)
    # Build initial map from deduped opportunities
    merged: dict[str, Opportunity] = {o.id: o for o in url_deduped.values()}

    # Process new opportunities and merge with existing ones by ID
    for incoming in new_opps:
        if incoming.id not in merged:
            # New opportunity - add it
            merged[incoming.id] = incoming
        else:
            # Existing opportunity - merge fields
            existing = merged[incoming.id]
            existing_data = existing.model_dump()

            # Merge top-level scalar fields
            for scalar in ("name", "institution_or_funder", "country", "degree_level", "official_url"):
                inc_val = getattr(incoming, scalar)
                if inc_val and not getattr(existing, scalar):
                    existing_data[scalar] = inc_val

            # Merge field list: EVIDENCED never overwritten by UNVERIFIED
            existing_fields_map: dict[str, dict] = {
                f["field_name"]: f for f in existing_data["fields"]
            }
            for inc_field in incoming.fields:
                fname = inc_field.field_name
                if fname in existing_fields_map:
                    ex = existing_fields_map[fname]
                    if (
                        ex["status"] == FieldStatus.EVIDENCED
                        and inc_field.status != FieldStatus.EVIDENCED
                    ):
                        continue  # protect EVIDENCED
                existing_fields_map[fname] = inc_field.model_dump()
            existing_data["fields"] = list(existing_fields_map.values())

            # Merge verification: prefer higher completeness_score
            if incoming.verification.completeness_score > existing.verification.completeness_score:
                existing_data["verification"] = incoming.verification.model_dump()

            # Merge funding if incoming is better
            if incoming.funding and (
                not existing.funding
                or incoming.funding.status == FieldStatus.EVIDENCED
            ):
                existing_data["funding"] = incoming.funding.model_dump()

            # Append new deadlines
            existing_deadline_descs = {d.description for d in existing.deadlines}
            for dl in incoming.deadlines:
                if dl.description not in existing_deadline_descs:
                    existing_data["deadlines"].append(dl.model_dump())

            merged[incoming.id] = Opportunity.model_validate(existing_data)

    return list(merged.values())


def reduce_diagnostics(
    current: list[DiagnosticEntry],
    new_entry: DiagnosticEntry,
) -> list[DiagnosticEntry]:
    """Append-only; zero raw web text allowed in DiagnosticEntry."""
    return current + [new_entry]


def reduce_assessments(
    current: list[EligibilityAssessment],
    new_assessments: list[EligibilityAssessment],
    top_5: list[str],
) -> tuple[list[EligibilityAssessment], list[str]]:
    """Single-write replace for assessments and top_5.

    Raises ValueError if assessments have already been written.
    """
    if current:
        raise ValueError("Assessments already written; single-write invariant violated")
    return new_assessments, top_5


def reduce_application(
    current: ApplicationPackage | None,
    new_pkg: ApplicationPackage,
) -> ApplicationPackage:
    """Staged replace: allow overwrite only with a fully-verified package.

    If current is None, accept new_pkg unconditionally.
    If current is set (e.g. pre-verification draft), replace only when new_pkg
    has all sop_sentences marked verified=True.
    """
    if current is None:
        return new_pkg
    unverified = [s for s in new_pkg.sop_sentences if not s.verified]
    if unverified:
        raise ValueError(
            f"ApplicationPackage has {len(unverified)} unverified sentences; "
            "run claim-ledger verification before committing"
        )
    return new_pkg
