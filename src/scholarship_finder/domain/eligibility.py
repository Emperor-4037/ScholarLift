"""Deterministic eligibility rule evaluation with exact values.

Every rule compares a student value against a stated requirement and returns
an :class:`EligibilityRule` carrying both numbers in its explanation.
Missing data is always UNKNOWN — never assumed to pass. The LLM only
supplies judgements (nationality/field text) passed in via ``judgements``.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from scholarship_finder.domain.gpa import normalize_gpa
from scholarship_finder.domain.models import (
    CandidateProfile,
    EligibilityResult,
    EligibilityRule,
    Opportunity,
    AssessmentStatus,
)

# Nationality demonym/alias map (small, language-level — not discipline data).
_NATIONALITY_ALIASES: dict[str, set[str]] = {
    "india": {"indian"}, "nigeria": {"nigerian"}, "china": {"chinese"},
    "germany": {"german"}, "canada": {"canadian"}, "pakistan": {"pakistani"},
    "bangladesh": {"bangladeshi"}, "brazil": {"brazilian"}, "usa": {"american", "us", "united states"},
    "uk": {"british", "united kingdom"}, "australia": {"australian"},
    "france": {"french"}, "italy": {"italian"}, "spain": {"spanish"},
    "japan": {"japanese"}, "kenya": {"kenyan"}, "ghana": {"ghanaian"},
    "ethiopia": {"ethiopian"}, "egypt": {"egyptian"}, "vietnam": {"vietnamese"},
    "indonesia": {"indonesian"}, "iran": {"iranian"}, "turkey": {"turkish"},
    "mexico": {"mexican"}, "south africa": {"south african"}, "korea": {"korean", "south korean"},
    "philippines": {"filipino"}, "sri lanka": {"sri lankan"}, "nepal": {"nepali"},
}

# Published concordance (approximate equivalents of an IELTS overall score).
# Used only when the student took a *different* test than required; the result
# is marked "estimate" in the explanation.
_IELTS_EQUIVALENTS: dict[str, list[tuple[float, float]]] = {
    # test: [(ielts_min, equivalent_min)]
    "toefl": [(4.0, 32), (5.0, 42), (5.5, 46), (6.0, 60), (6.5, 79), (7.0, 94), (7.5, 102), (8.0, 110), (9.0, 120)],
    "pte": [(4.0, 30), (5.0, 36), (5.5, 42), (6.0, 50), (6.5, 58), (7.0, 65), (7.5, 73), (8.0, 79), (9.0, 86)],
    "duolingo": [(4.0, 55), (5.0, 65), (5.5, 75), (6.0, 85), (6.5, 95), (7.0, 105), (7.5, 115), (8.0, 125), (9.0, 145)],
}

_LEVEL_ORDER = {"school": 0, "diploma": 1, "bachelors": 2, "masters": 3, "phd": 4, "postdoc": 5}
_LEVEL_ALIASES = {
    "bachelor": "bachelors", "bachelors": "bachelors", "undergraduate": "bachelors",
    "bsc": "bachelors", "ba": "bachelors", "b.tech": "bachelors", "btech": "bachelors",
    "master": "masters", "masters": "masters", "msc": "masters", "ma": "masters",
    "m.tech": "masters", "mtech": "masters", "graduate": "masters", "postgraduate": "masters",
    "phd": "phd", "doctoral": "phd", "doctorate": "phd", "dphil": "phd",
    "diploma": "diploma", "postdoc": "postdoc", "school": "school", "secondary": "school",
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_LEVEL_PATTERNS: list[tuple[re.Pattern, set[str]]] = [
    (re.compile(r"\bpost ?doc"), {"postdoc"}),
    (re.compile(r"\b(phd|ph d|doctoral|doctorate|dphil|doctor of)"), {"phd"}),
    (re.compile(r"\b(master|masters|msc|m sc|mtech|m tech|mba|mph|llm|meng|mres|ma)\b"), {"masters"}),
    (re.compile(r"\b(postgraduate|post graduate|graduate|grad)\b"), {"masters", "phd"}),
    (re.compile(r"\b(bachelor|bachelors|undergraduate|bsc|b sc|btech|b tech|ba|beng)\b"), {"bachelors"}),
    (re.compile(r"\bdiploma\b"), {"diploma"}),
    (re.compile(r"\b(all levels|any level|all degree|any degree|all study levels)\b"),
     {"bachelors", "masters", "phd"}),
]


def degree_levels_of(s: str) -> set[str]:
    """Degree levels a free-text label covers ("Master's degree" → {masters})."""
    n = _norm(s)
    if n in _LEVEL_ALIASES:
        return {_LEVEL_ALIASES[n]}
    out: set[str] = set()
    for pat, lv in _LEVEL_PATTERNS:
        if pat.search(n):
            out |= lv
    return out


_levels = degree_levels_of


def _level(s: str) -> str:
    lv = _levels(s)
    if len(lv) == 1:
        return next(iter(lv))
    return _LEVEL_ALIASES.get(_norm(s), _norm(s))


_COUNTRY_ALIASES: dict[str, set[str]] = {
    "united kingdom": {"uk", "great britain", "britain", "england", "scotland", "wales"},
    "united states": {"usa", "us", "united states of america", "america"},
    "germany": {"deutschland"},
    "netherlands": {"holland", "the netherlands"},
    "south korea": {"korea", "republic of korea"},
    "uae": {"united arab emirates"},
}
_ANY_HOST = {"international", "multiple", "various", "any", "worldwide", "global",
             "around the world", "multiple countries", "europe", "european union", "eu"}


def _country_keys(s: str) -> set[str]:
    n = _norm(s)
    keys = {n} if n else set()
    for canon, aliases in _COUNTRY_ALIASES.items():
        if n == canon or n in aliases:
            keys |= {canon} | aliases
    return keys


def _host_matches(hosts: set[str], targets: set[str]) -> bool | None:
    """True if a host covers a target, None if hosts are only generic."""
    target_keys = set().union(*[_country_keys(t) for t in targets]) if targets else set()
    generic = False
    for h in hosts:
        parts = [p.strip() for p in re.split(r"[,;/&]| and ", h) if p.strip()]
        for p in parts:
            keys = _country_keys(p)
            if keys & target_keys or any(t in _norm(p).split() or t == _norm(p)
                                         for t in target_keys):
                return True
            if _norm(p) in _ANY_HOST:
                generic = True
    return None if generic else False


_ANY_FIELD = re.compile(
    r"\b(all (fields|disciplines|subjects|areas|academic)|any (field|discipline|subject)|"
    r"open to all|all study programmes|all programmes|all programs|multidisciplinary|"
    r"no restriction)", re.I)


def _tokens(s: str) -> set[str]:
    return {t for t in _norm(s).split() if len(t) > 2}


def _nationality_terms(nat: str) -> set[str]:
    n = _norm(nat)
    terms = {n} if n else set()
    for country, aliases in _NATIONALITY_ALIASES.items():
        if n == country or n in aliases:
            terms.add(country)
            terms |= aliases
    return terms


def _student_us_gpa(profile: CandidateProfile) -> tuple[float | None, float | None, str]:
    """Return (us_gpa, source_value/scale note) — exact numbers for display."""
    edu = profile.primary_education()
    if edu is None or edu.cgpa_value is None or not edu.cgpa_scale:
        return None, None, ""
    if edu.normalized_gpa and edu.normalized_gpa.value is not None:
        note = f"{edu.cgpa_value}/{edu.cgpa_scale} ≈ {edu.normalized_gpa.value:.2f}/4.0"
        return edu.normalized_gpa.value, edu.cgpa_value, note
    if edu.grade_system == "lower_is_better":
        try:
            res = normalize_gpa(edu.cgpa_value, edu.cgpa_scale, target="german_1to4")
            return res.value, edu.cgpa_value, f"{edu.cgpa_value}/{edu.cgpa_scale} (lower-better) ≈ {res.value:.2f}"
        except ValueError:
            return None, None, ""
    try:
        res = normalize_gpa(edu.cgpa_value, edu.cgpa_scale, target="us_4point")
        note = f"{edu.cgpa_value}/{edu.cgpa_scale} ≈ {res.value:.2f}/4.0"
        return res.value, edu.cgpa_value, note
    except ValueError:
        return None, None, ""


def _required_us_gpa(opp: Opportunity) -> float | None:
    crit = opp.eligibility
    if crit.min_gpa_value is None:
        return None
    if crit.min_gpa_scale and crit.min_gpa_scale != 4.0:
        try:
            return normalize_gpa(crit.min_gpa_value, crit.min_gpa_scale, target="us_4point").value
        except ValueError:
            return None
    return crit.min_gpa_value


def _student_test_score(profile: CandidateProfile, required_test: str) -> tuple[float | None, str, bool]:
    """Return (score_on_required_scale, note, is_estimate)."""
    req = _norm(required_test)
    for lt in profile.language_tests:
        if _norm(lt.test_name) == req and lt.overall_score is not None:
            return lt.overall_score, lt.test_name, False
    # Different test → concordance estimate
    for lt in profile.language_tests:
        test = _norm(lt.test_name)
        if lt.overall_score is None:
            continue
        for key, table in _IELTS_EQUIVALENTS.items():
            if key in req and key != test:
                continue   # required test isn't one we can convert to
        if "ielts" in req and test in _IELTS_EQUIVALENTS:
            # Convert student score → IELTS equivalent
            table = _IELTS_EQUIVALENTS[test]
            est = 0.0
            for ielts_min, eq_min in table:
                if lt.overall_score >= eq_min:
                    est = ielts_min
            return est, f"{lt.test_name} {lt.overall_score} ≈ IELTS {est} (estimate)", True
        if test == "ielts" and "ielts" not in req:
            for key, table in _IELTS_EQUIVALENTS.items():
                if key in req:
                    eq = 0.0
                    for ielts_min, eq_min in table:
                        if lt.overall_score >= ielts_min:
                            eq = eq_min
                    return eq, f"IELTS {lt.overall_score} ≈ {required_test.upper()} {eq} (estimate)", True
    return None, "", False


def evaluate_rules(
    profile: CandidateProfile,
    opp: Opportunity,
    judgements: dict[str, Any] | None = None,
) -> list[EligibilityRule]:
    """Evaluate every eligibility rule for one opportunity.

    ``judgements`` may carry ``nationality_judgement`` and ``field_judgement``
    booleans supplied by the LLM for text-only requirements.
    """
    judgements = judgements or {}
    crit = opp.eligibility
    rules: list[EligibilityRule] = []

    # ── Nationality ──────────────────────────────────────────────────────────
    nat = profile.nationality or profile.current_country
    nat_terms = _nationality_terms(nat)
    if crit.nationalities:
        req_terms: set[str] = set()
        for n in crit.nationalities:
            req_terms |= _nationality_terms(n)
        if nat_terms:
            hit = bool(nat_terms & req_terms) or any(
                n in _norm(c) for n in nat_terms for c in crit.nationalities
            )
            rules.append(EligibilityRule(
                rule_name="nationality", label="Nationality",
                result=EligibilityResult.MET if hit else EligibilityResult.UNMET,
                candidate_value=nat or "not stated",
                required_value=", ".join(crit.nationalities),
                explanation=(f"Student nationality {nat or 'not stated'} vs required "
                             f"[{', '.join(crit.nationalities)}]"),
                blocking=True, closable=False,
            ))
        else:
            rules.append(EligibilityRule(
                rule_name="nationality", label="Nationality",
                result=EligibilityResult.UNKNOWN,
                required_value=", ".join(crit.nationalities),
                explanation="Student nationality not on file",
                blocking=True, closable=True,
            ))
    elif crit.open_to_international is False:
        if nat_terms:
            rules.append(EligibilityRule(
                rule_name="nationality", label="Nationality",
                result=EligibilityResult.UNMET,
                candidate_value=nat or "not stated",
                required_value="domestic students only",
                explanation="Opportunity is for domestic students only",
                blocking=True, closable=False,
            ))
        else:
            rules.append(EligibilityRule(
                rule_name="nationality", label="Nationality",
                result=EligibilityResult.UNKNOWN,
                required_value="domestic students only",
                explanation="Student nationality not on file",
                blocking=True, closable=True,
            ))
    elif crit.nationality_text:
        j = judgements.get("nationality_judgement")
        if j is True:
            res = EligibilityResult.MET
        elif j is False:
            res = EligibilityResult.UNMET
        else:
            res = EligibilityResult.UNKNOWN
        rules.append(EligibilityRule(
            rule_name="nationality", label="Nationality",
            result=res,
            candidate_value=nat or "not stated",
            required_value=crit.nationality_text,
            explanation=f"Region text '{crit.nationality_text}' vs {nat or 'unknown'}",
            blocking=True, closable=(res == EligibilityResult.UNKNOWN),
        ))
    elif crit.open_to_international is True:
        rules.append(EligibilityRule(
            rule_name="nationality", label="Nationality",
            result=EligibilityResult.MET,
            candidate_value=nat or "not stated",
            required_value="open to international students",
            explanation="Open to international students",
        ))

    # ── Degree level ─────────────────────────────────────────────────────────
    target = _level(profile.preferences.degree_level)
    req_levels: set[str] = set()
    for d in crit.degree_levels or []:
        req_levels |= _levels(d)
    if crit.degree_levels and req_levels:
        if target:
            hit = target in req_levels
            rules.append(EligibilityRule(
                rule_name="degree_level", label="Degree level",
                result=EligibilityResult.MET if hit else EligibilityResult.UNMET,
                candidate_value=profile.preferences.degree_level or "not stated",
                required_value=", ".join(crit.degree_levels),
                explanation=(f"Student target {profile.preferences.degree_level or '?'} vs "
                             f"levels [{', '.join(crit.degree_levels)}]"),
                blocking=True, closable=False,
            ))
        else:
            rules.append(EligibilityRule(
                rule_name="degree_level", label="Degree level",
                result=EligibilityResult.UNKNOWN,
                required_value=", ".join(crit.degree_levels),
                explanation="Student target degree level not on file",
                blocking=True, closable=True,
            ))

    # ── Field of study ───────────────────────────────────────────────────────
    student_fields = profile.study_fields()
    student_tokens = set().union(*[_tokens(f) for f in student_fields]) if student_fields else set()
    if crit.fields_of_study:
        req_tokens = set().union(*[_tokens(f) for f in crit.fields_of_study])
        stop = {"and", "the", "for", "all", "any", "related", "fields", "studies", "study", "or"}
        overlap = (student_tokens & req_tokens) - stop
        if any(_ANY_FIELD.search(f) for f in crit.fields_of_study):
            res, how = EligibilityResult.MET, "open to all fields"
        elif overlap:
            res = EligibilityResult.MET
            how = f"field token overlap: {sorted(overlap)}"
        else:
            j = judgements.get("field_judgement")
            if j is True:
                res, how = EligibilityResult.MET, "LLM judged fields compatible"
            elif j is False:
                res, how = EligibilityResult.UNMET, "no token overlap; LLM judged incompatible"
            else:
                res, how = EligibilityResult.UNKNOWN, "no direct field token overlap (unjudged)"
        rules.append(EligibilityRule(
            rule_name="field_of_study", label="Field of study",
            result=res,
            candidate_value=", ".join(student_fields[:3]) or "not stated",
            required_value=", ".join(crit.fields_of_study[:4]),
            explanation=f"{how} (student: {', '.join(student_fields[:3]) or '?'})",
            blocking=(res == EligibilityResult.UNMET),
            closable=False,
        ))

    # ── GPA ──────────────────────────────────────────────────────────────────
    req_gpa = _required_us_gpa(opp)
    stu_gpa, raw_gpa, gpa_note = _student_us_gpa(profile)
    if crit.min_gpa_value is not None and req_gpa is not None:
        if stu_gpa is not None:
            margin = stu_gpa - req_gpa
            rules.append(EligibilityRule(
                rule_name="gpa", label="GPA minimum",
                result=EligibilityResult.MET if margin >= 0 else EligibilityResult.UNMET,
                candidate_value=gpa_note or f"{stu_gpa:.2f}/4.0",
                required_value=crit.min_gpa_text or f"{crit.min_gpa_value}/{crit.min_gpa_scale or 4.0}",
                explanation=(f"GPA {gpa_note or f'{stu_gpa:.2f}/4.0'} vs required "
                             f"{crit.min_gpa_text or f'{req_gpa:.2f}'} → margin {margin:+.2f}"),
                blocking=margin < -0.3,
                closable=margin >= -0.3,
                gap_size=round(-margin, 2) if margin < 0 else None,
            ))
        else:
            rules.append(EligibilityRule(
                rule_name="gpa", label="GPA minimum",
                result=EligibilityResult.UNKNOWN,
                required_value=crit.min_gpa_text or f"{crit.min_gpa_value}",
                explanation="Student GPA not on file",
                blocking=False, closable=True,
            ))

    # ── Destination fit (deterministic) ──────────────────────────────────────
    targets = {c.lower() for c in (profile.preferences.countries or [])}
    hosts = {h.lower().strip() for h in (opp.host_countries or []) if h.strip()}
    if opp.country and opp.country.strip():
        hosts.add(opp.country.strip().lower())
    if targets and hosts:
        on = _host_matches(hosts, targets)
        res = {True: EligibilityResult.MET, False: EligibilityResult.UNMET,
               None: EligibilityResult.UNKNOWN}[on]
        verdict = {True: "in", False: "not in", None: "may include"}[on]
        rules.append(EligibilityRule(
            rule_name="destination", label="Destination",
            result=res,
            candidate_value=", ".join(sorted(targets)),
            required_value=", ".join(sorted(hosts)),
            explanation=f"host {', '.join(sorted(hosts))} {verdict} target countries",
            blocking=on is False, closable=False))
    elif targets:
        rules.append(EligibilityRule(
            rule_name="destination", label="Destination",
            result=EligibilityResult.UNKNOWN,
            candidate_value=", ".join(sorted(targets)),
            required_value="host country unstated",
            explanation="host country not stated on page",
            blocking=False, closable=False))

    # ── Language requirements ────────────────────────────────────────────────
    for lr in crit.language_requirements:
        score, note, est = _student_test_score(profile, lr.test)
        if score is not None:
            ok = score >= lr.min_score
            short = round(lr.min_score - score, 2) if not ok else None
            rules.append(EligibilityRule(
                rule_name=f"language_{_norm(lr.test)}", label=f"Language: {lr.test}",
                result=EligibilityResult.MET if ok else EligibilityResult.UNMET,
                candidate_value=note or str(score),
                required_value=f"{lr.test} ≥ {lr.min_score}",
                explanation=(f"{note or score} vs required {lr.test} ≥ {lr.min_score}"
                             + (" (estimate)" if est else "")),
                blocking=not ok and (short or 99) > 1.0,
                closable=not ok or est,
                gap_size=short,
            ))
        else:
            rules.append(EligibilityRule(
                rule_name=f"language_{_norm(lr.test)}", label=f"Language: {lr.test}",
                result=EligibilityResult.UNMET,
                required_value=f"{lr.test} ≥ {lr.min_score}",
                explanation="No language test on file (non-blocking, closable)",
                blocking=False, closable=True,
            ))

    # ── Work experience ──────────────────────────────────────────────────────
    if crit.min_work_experience_months:
        have = profile.total_experience_months()
        need = crit.min_work_experience_months
        short = need - have
        rules.append(EligibilityRule(
            rule_name="work_experience", label="Work experience",
            result=EligibilityResult.MET if have >= need else EligibilityResult.UNMET,
            candidate_value=f"{have} months",
            required_value=f"{need} months",
            explanation=f"{have} months experience vs required {need}",
            blocking=short > 12,
            closable=short <= 12,
            gap_size=short if short > 0 else None,
        ))

    # ── Deadline ─────────────────────────────────────────────────────────────
    dl = opp.next_deadline()
    if dl and dl.iso_date:
        try:
            d = date.fromisoformat(dl.iso_date[:10])
            days = (d - date.today()).days
            rules.append(EligibilityRule(
                rule_name="deadline", label="Deadline",
                result=EligibilityResult.MET if days >= 0 else EligibilityResult.UNMET,
                candidate_value="",
                required_value=dl.iso_date,
                explanation=(f"Deadline {dl.iso_date} is in {days} days"
                             if days >= 0 else
                             f"Deadline {dl.iso_date} passed {-days} days ago — "
                             "watch for the next cycle"),
                blocking=days < 0,
                # Most awards recur yearly: a deadline passed within the last
                # year means "apply next cycle", not "never eligible".
                closable=-365 <= days < 0,
            ))
        except ValueError:
            pass
    elif dl and dl.is_past:
        rules.append(EligibilityRule(
            rule_name="deadline", label="Deadline",
            result=EligibilityResult.UNMET,
            required_value=dl.date or "deadline",
            explanation="Deadline has passed (as stated on page)",
            blocking=True, closable=False,
        ))

    # ── Funding vs need (informational) ──────────────────────────────────────
    need = profile.financial_need.level
    if need in ("full", "partial") and opp.funding:
        fl = opp.funding.level.value if opp.funding.level else "UNKNOWN"
        adequate = opp.funding.level and (
            opp.funding.level.value in ("FULL_TUITION_STIPEND", "FULL_TUITION")
            or (need == "partial" and opp.funding.level.value == "PARTIAL")
        )
        rules.append(EligibilityRule(
            rule_name="funding_fit", label="Funding vs need",
            result=EligibilityResult.MET if adequate else EligibilityResult.UNKNOWN,
            candidate_value=f"needs {need} funding",
            required_value=fl,
            explanation=f"Student needs {need} funding; offer is {fl} (informational)",
            blocking=False, closable=False,
        ))

    return rules


def classify_status(rules: list[EligibilityRule]) -> AssessmentStatus:
    """eligible / likely_eligible / near_miss / ineligible.

    ineligible   — a hard, non-closable blocker (nationality, degree level,
                   passed deadline, GPA short >0.3, experience short >12mo)
    near_miss    — a failed requirement that is closable (GPA short ≤0.3,
                   experience short ≤12mo, missing/low language test)
    likely_eligible — nothing failed, only missing data (≤2 UNKNOWN)
    eligible     — every requirement met
    """
    if not rules:
        return "requirements_unverified"
    unmet = [r for r in rules if r.result == EligibilityResult.UNMET]
    unknown = [r for r in rules if r.result == EligibilityResult.UNKNOWN]
    met = [r for r in rules if r.result == EligibilityResult.MET]
    if any(r.blocking and not r.closable for r in unmet):
        return "ineligible"
    if unmet:
        return "near_miss"
    # likely_eligible needs ≥2 hard MET rules incl. one of nat/degree/field
    core = {"nationality", "degree_level", "field_of_study"}
    if len(met) >= 2 and any(r.rule_name in core for r in met):
        return "likely_eligible" if len(unknown) <= 2 else "near_miss"
    return "requirements_unverified"
