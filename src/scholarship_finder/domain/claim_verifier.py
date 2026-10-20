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


def extract_proper_nouns(text: str, stop_list: frozenset[str] | None = None) -> set[str]:
    """Extract candidate proper nouns from *text*.

    Returns lower-cased multi-word Title Case sequences that are NOT in the
    academic stop list, plus notable acronyms.
    """
    stop = stop_list if stop_list is not None else _ACADEMIC_STOP_SET

    found: set[str] = set()

    for m in _PROPER_NOUN_RE.finditer(text):
        token = m.group(1)
        if token.lower() not in stop:
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
    for fid in cited_fact_ids:
        if fid not in fact_pack:
            missing_ids.append(fid)

    if missing_ids:
        failures.append(f"Unknown fact IDs: {missing_ids}")

    # Union of all cited fact values (for guards 2 & 3)
    cited_values_text = " ".join(
        fact_pack[fid] for fid in cited_fact_ids if fid in fact_pack
    ).lower()

    # Guard 2: Numeric token grounding
    nums_in_sentence = extract_numeric_tokens(sentence_text)
    for num in nums_in_sentence:
        # Normalise: remove commas, spaces before %
        norm_num = num.replace(",", "").replace(" %", "%").replace(" ", "")
        if norm_num not in cited_values_text and num not in cited_values_text:
            ungrounded_numbers.append(num)

    if ungrounded_numbers:
        failures.append(f"Numeric tokens not grounded in cited facts: {ungrounded_numbers}")

    # Guard 3: Proper-noun allow-list
    proper_nouns = extract_proper_nouns(sentence_text)
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
