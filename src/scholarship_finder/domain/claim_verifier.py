"""Stage-2 SOP Claim-Ledger Verifier (architecture §2.5).

Three deterministic guards per SOP sentence:
  1. Referential Integrity  — every cited fact_id exists in the allowed-fact pack
  2. Numeric Token Guard    — every numeric token in the sentence appears in its cited facts
  3. Proper-Noun Allow-List — capitalized multi-word tokens must match facts or [CANDIDATE_NAME]

Sentences failing verification get one targeted fast-tier repair, then are stripped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Standard academic / common proper nouns that should not trigger guard 3 ──

_ACADEMIC_STOP_SET: frozenset[str] = frozenset({
    "university", "department", "school", "college", "institute", "faculty",
    "master", "masters", "bachelor", "bachelors", "phd", "doctor", "doctorate",
    "professor", "statement", "purpose", "dear", "committee", "program",
    "scholarship", "fellowship", "grant", "award", "international", "graduate",
    "undergraduate", "postgraduate", "research", "engineering", "science",
    "technology", "management", "business", "arts", "humanities", "medicine",
    # Multi-word phrases (lowercased)
    "graduate school", "graduate program", "graduate studies",
    "computer science", "artificial intelligence", "machine learning",
    "data science", "deep learning", "natural language processing",
    "computer vision", "software engineering", "information technology",
    "electrical engineering", "mechanical engineering",
    "united states", "united kingdom", "united arab emirates",
    "european union", "north america", "south asia",
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
})

# Sentence-initial transition words that should never trigger Guard 3
# when they appear as the first Title-Case word of a sentence.
_SENTENCE_INITIAL_TRANSITIONS: frozenset[str] = frozenset({
    "During", "In", "Building", "Through", "Furthermore", "Additionally",
    "Moreover", "However", "Therefore", "Consequently", "Subsequently",
    "Meanwhile", "Initially", "Finally", "Notably", "Specifically",
    "Motivated", "Driven", "Having", "Upon", "With", "By", "As",
    "Since", "While", "Although", "Because", "If", "When", "After",
    "Before", "Throughout", "Beyond", "Within", "Among", "Between",
})

# ── Fact citation pattern ─────────────────────────────────────────────────────

_FACT_CITE_RE = re.compile(r"\[?\bFACT_\d{3}\b\]?")

# ── Numeric token regex ────────────────────────────────────────────────────────

_NUMERIC_RE = re.compile(
    r"""
    \b
    (?:
        \d{4}                              # year-like: 2024
        |\d+\.\d+                          # decimal: 3.7, 8.2
        |\d+\s*%                           # percentage: 90%
        |\$\s*[\d,]+(?:\.\d+)?            # dollar: $50,000
        |[\d,]+(?:\.\d+)?(?:\s*(?:USD|EUR|GBP|INR|SGD|CAD|AUD))?  # amount with optional currency
        |\d+                               # plain integer
    )
    \b
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Structural ordinal patterns that are never "ungrounded" numbers
# e.g. "1st", "2nd", "3rd", "4th"
_ORDINAL_RE = re.compile(r"^\d+(?:st|nd|rd|th)$", re.IGNORECASE)

# ── Proper-noun extractor: sequences of Title-Case words ─────────────────────

_PROPER_NOUN_RE = re.compile(
    r"\b([A-Z][a-z]{1,}(?:\s+[A-Z][a-z]{1,})+)\b"   # two or more Title Case words
)

# Single-word proper nouns (all-caps acronyms or long proper names)
_ACRONYM_RE = re.compile(r"\b([A-Z]{2,})\b")


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class ClaimVerificationResult:
    sentence_index: int
    passed: bool
    failures: list[str] = field(default_factory=list)
    missing_fact_ids: list[str] = field(default_factory=list)
    ungrounded_numbers: list[str] = field(default_factory=list)
    ungrounded_nouns: list[str] = field(default_factory=list)


# ── Core helpers ──────────────────────────────────────────────────────────────

def extract_numeric_tokens(text: str) -> set[str]:
    """Extract all numeric-looking tokens from *text*, normalised to lower-case."""
    return {m.group().strip().lower() for m in _NUMERIC_RE.finditer(text)}


def _normalize_number(num: str) -> str:
    """Normalize a numeric token for loose comparison (e.g. '3.80' → '3.8')."""
    num = num.replace(",", "").replace(" %", "%").replace(" ", "")
    # Normalize trailing zeros after decimal: 3.80 → 3.8, 3.00 → 3.0
    try:
        f = float(num.rstrip("%"))
        if "." in num:
            normalized = f"{f:g}"
            if "%" in num:
                normalized += "%"
            return normalized
    except ValueError:
        pass
    return num


def extract_proper_nouns(text: str, stop_list: frozenset[str] | None = None) -> set[str]:
    """Extract candidate proper nouns from *text*.

    Returns lower-cased multi-word Title Case sequences that are NOT in the
    academic stop list, plus notable acronyms.

    Sentence-initial Title-Case words (transitions like "During", "In", "Building")
    are excluded when they appear as the leading word of the first multi-word group.
    """
    stop = stop_list if stop_list is not None else _ACADEMIC_STOP_SET

    found: set[str] = set()

    for m in _PROPER_NOUN_RE.finditer(text):
        token = m.group(1)
        if token.lower() in stop:
            continue
        # Check if the first word is a sentence-initial transition
        first_word = token.split()[0]
        if first_word in _SENTENCE_INITIAL_TRANSITIONS:
            # Drop the leading transition word; check if residual is worth keeping
            rest = " ".join(token.split()[1:])
            if rest and rest.lower() not in stop:
                found.add(token)
            # else: skip entirely — the transition dominates
            continue
        found.add(token)

    for m in _ACRONYM_RE.finditer(text):
        token = m.group(1)
        if token not in {"I", "A", "GPA", "SOP", "URL", "LLM", "AI", "ML", "US", "UK", "EU",
                          "PhD", "MS", "BS", "BE", "IELTS", "TOEFL", "PTE", "GRE", "GMAT"}:
            found.add(token)

    return found


# ── Sentence-level verification ───────────────────────────────────────────────

def verify_sentence(
    sentence_text: str,
    cited_fact_ids: list[str],
    fact_pack: dict[str, str],
    candidate_name_placeholder: str = "[CANDIDATE_NAME]",
) -> ClaimVerificationResult:
    """Verify one SOP sentence against the allowed-fact pack.

    Args:
        sentence_text:          The sentence to check.
        cited_fact_ids:         Fact IDs the sentence claims to rely on.
        fact_pack:              Mapping of fact_id → fact_value_text.
        candidate_name_placeholder: Allowed placeholder for the candidate's name.

    Returns:
        ClaimVerificationResult with pass/fail and details.
    """
    failures: list[str] = []
    missing_ids: list[str] = []
    ungrounded_numbers: list[str] = []
    ungrounded_nouns: list[str] = []

    # Guard 1: Referential integrity
    # First, extract all FACT_XXX citation IDs present in the sentence text
    sentence_fact_ids = _FACT_CITE_RE.findall(sentence_text)
    # Strip brackets to get bare IDs
    sentence_fact_ids_clean = [
        fid.strip("[]").strip() for fid in sentence_fact_ids
    ]
    # Merge with explicitly supplied cited_fact_ids
    all_cited = list(dict.fromkeys(cited_fact_ids + sentence_fact_ids_clean))

    for fid in all_cited:
        if fid not in fact_pack:
            missing_ids.append(fid)

    if missing_ids:
        failures.append(f"Unknown fact IDs: {missing_ids}")

    # Union of all cited fact values (for guards 2 & 3)
    cited_values_text = " ".join(
        fact_pack[fid] for fid in all_cited if fid in fact_pack
    ).lower()

    # Strip FACT_XXX citation tokens from the sentence BEFORE running Guards 2 & 3
    # so citation digits (e.g. "001") are never flagged as ungrounded numbers.
    cleaned_sentence = _FACT_CITE_RE.sub("", sentence_text).strip()

    # Guard 2: Numeric token grounding
    nums_in_sentence = extract_numeric_tokens(cleaned_sentence)
    for num in nums_in_sentence:
        # Skip structural ordinals: 1st, 2nd, 3rd, 4th, etc.
        if _ORDINAL_RE.match(num):
            continue
        # Normalise: remove commas, spaces before %
        norm_num = _normalize_number(num)
        if norm_num not in cited_values_text and num not in cited_values_text:
            # Also try the raw form after simple comma/space strip
            raw_norm = num.replace(",", "").replace(" %", "%").replace(" ", "")
            if raw_norm not in cited_values_text:
                ungrounded_numbers.append(num)

    if ungrounded_numbers:
        failures.append(f"Numeric tokens not grounded in cited facts: {ungrounded_numbers}")

    # Guard 3: Proper-noun allow-list
    proper_nouns = extract_proper_nouns(cleaned_sentence)
    for noun in proper_nouns:
        if (
            noun.lower() not in cited_values_text
            and candidate_name_placeholder not in sentence_text
        ):
            ungrounded_nouns.append(noun)

    if ungrounded_nouns:
        failures.append(f"Proper nouns not in fact pack: {ungrounded_nouns}")

    return ClaimVerificationResult(
        sentence_index=-1,   # caller sets the real index
        passed=len(failures) == 0,
        failures=failures,
        missing_fact_ids=missing_ids,
        ungrounded_numbers=ungrounded_numbers,
        ungrounded_nouns=ungrounded_nouns,
    )


def verify_sop(
    sentences: list[tuple[str, list[str]]],
    fact_pack: dict[str, str],
) -> list[ClaimVerificationResult]:
    """Verify all sentences in the SOP.

    Args:
        sentences:  List of (sentence_text, cited_fact_ids).
        fact_pack:  {fact_id: value_text} allowed-fact pack.

    Returns:
        List of ClaimVerificationResult, one per sentence, with sentence_index set.
    """
    results: list[ClaimVerificationResult] = []
    for idx, (text, ids) in enumerate(sentences):
        r = verify_sentence(text, ids, fact_pack)
        r.sentence_index = idx
        results.append(r)
    return results


def get_failing_sentence_indices(results: list[ClaimVerificationResult]) -> list[int]:
    """Return indices of all sentences that failed verification."""
    return [r.sentence_index for r in results if not r.passed]


# ── [F#] citation scheme (current SOP format) ────────────────────────────────

_F_CITE_RE = re.compile(r"\[F(\d+)\]")



def parse_cited_ids(sentence_text: str) -> list[str]:
    """Extract ``F#`` fact ids cited in a sentence."""
    return [f"F{m}" for m in _F_CITE_RE.findall(sentence_text)]


def strip_citations(sentence_text: str) -> str:
    """Remove ``[F#]`` markers for display text."""
    return _F_CITE_RE.sub("", sentence_text).replace("  ", " ").strip()


def verify_sentence_f(
    sentence_text: str,
    fact_pack: dict[str, str],
    extra_allowed: frozenset[str] | None = None,
    candidate_name_placeholder: str = "[CANDIDATE_NAME]",
) -> ClaimVerificationResult:
    """Verify one sentence in the ``[F#]`` scheme.

    Rules (plan): cited ids must exist; numbers and proper nouns must appear in
    the cited facts (or the opportunity name/provider via ``extra_allowed``);
    uncited sentences pass only if they contain no numbers/proper nouns.
    """
    failures: list[str] = []
    missing: list[str] = []
    bad_nums: list[str] = []
    bad_nouns: list[str] = []

    cited = parse_cited_ids(sentence_text)
    for fid in cited:
        if fid not in fact_pack:
            missing.append(fid)
    if missing:
        failures.append(f"Unknown fact ids: {missing}")

    cited_text = " ".join(fact_pack[f] for f in cited if f in fact_pack).lower()
    allowed = cited_text + " " + " ".join(extra_allowed or ()).lower()

    cleaned = strip_citations(sentence_text)
    for num in extract_numeric_tokens(cleaned):
        if _ORDINAL_RE.match(num):
            continue
        norm = _normalize_number(num)
        raw = num.replace(",", "").replace(" %", "%").replace(" ", "")
        if norm not in allowed and num not in allowed and raw not in allowed:
            bad_nums.append(num)
    if bad_nums:
        failures.append(f"Numbers not grounded in cited facts: {bad_nums}")

    for noun in extract_proper_nouns(cleaned):
        if noun.lower() not in allowed and candidate_name_placeholder not in cleaned:
            bad_nouns.append(noun)
    if bad_nouns:
        failures.append(f"Proper nouns not grounded: {bad_nouns}")

    return ClaimVerificationResult(
        sentence_index=-1,
        passed=len(failures) == 0,
        failures=failures,
        missing_fact_ids=missing,
        ungrounded_numbers=bad_nums,
        ungrounded_nouns=bad_nouns,
    )


def verify_sop_f(
    sentences: list[str],
    fact_pack: dict[str, str],
    extra_allowed: frozenset[str] | None = None,
) -> list[ClaimVerificationResult]:
    """Verify all ``[F#]``-cited sentences; index set per result."""
    out = []
    for i, s in enumerate(sentences):
        r = verify_sentence_f(s, fact_pack, extra_allowed=extra_allowed)
        r.sentence_index = i
        out.append(r)
    return out
