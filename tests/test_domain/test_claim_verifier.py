"""Tests for SOP claim-ledger verifier (Stage 2 anti-hallucination)."""
from __future__ import annotations

import pytest

from scholarship_finder.domain.claim_verifier import (
    ClaimVerificationResult,
    extract_numeric_tokens,
    extract_proper_nouns,
    get_failing_sentence_indices,
    verify_sentence,
    verify_sop,
)

FACT_PACK = {
    "f1": "GPA 8.2 on a 10-point scale, equivalent to approximately 3.7 on US 4.0 scale",
    "f2": "DAAD Scholarship: full tuition coverage plus stipend of 861 euros per month",
    "f3": "Sentiment Analyser project achieving 94% accuracy on Amazon reviews dataset",
    "f4": "Minimum GPA requirement is 3.0 GPA on a 4.0 scale",
    "f5": "Program at Technical University of Munich in Computer Science",
}


class TestExtractNumericTokens:
    def test_extracts_decimal(self):
        tokens = extract_numeric_tokens("My GPA is 3.7.")
        assert "3.7" in tokens

    def test_extracts_percentage(self):
        tokens = extract_numeric_tokens("Achieved 94% accuracy.")
        assert any("94" in t for t in tokens)

    def test_extracts_year(self):
        tokens = extract_numeric_tokens("Starting in 2025.")
        assert "2025" in tokens

    def test_extracts_currency(self):
        tokens = extract_numeric_tokens("Funding of $50,000 per year.")
        assert any("50" in t for t in tokens)

    def test_no_numbers(self):
        tokens = extract_numeric_tokens("No numbers here whatsoever.")
        assert len(tokens) == 0


class TestExtractProperNouns:
    def test_finds_multi_word(self):
        nouns = extract_proper_nouns("I studied at Technical University of Munich.")
        # "Technical University" or "Technical University of Munich" might be found
        assert any("Technical" in n for n in nouns)

    def test_ignores_stop_list(self):
        nouns = extract_proper_nouns("I applied to the Graduate School of Computer Science.")
        # Graduate School and Computer Science are in the stop list
        assert not any(n in ("Computer Science", "Graduate School") for n in nouns)

    def test_single_word_sentence_start_not_flagged(self):
        nouns = extract_proper_nouns("My academic background is in computer science.")
        assert len(nouns) == 0


class TestVerifySentence:
    def test_all_pass(self):
        result = verify_sentence(
            sentence_text="My GPA of 8.2 reflects my academic excellence.",
            cited_fact_ids=["f1"],
            fact_pack=FACT_PACK,
        )
        assert result.passed is True

    def test_missing_fact_id(self):
        result = verify_sentence(
            sentence_text="My GPA is good.",
            cited_fact_ids=["f99"],  # does not exist
            fact_pack=FACT_PACK,
        )
        assert result.passed is False
        assert "f99" in result.missing_fact_ids

    def test_ungrounded_number(self):
        result = verify_sentence(
            sentence_text="I have a GPA of 9.9.",  # 9.9 not in f1
            cited_fact_ids=["f1"],
            fact_pack=FACT_PACK,
        )
        assert result.passed is False
        assert any("9.9" in n for n in result.ungrounded_numbers)

    def test_candidate_name_placeholder_allowed(self):
        result = verify_sentence(
            sentence_text="[CANDIDATE_NAME] achieved a GPA of 8.2.",
            cited_fact_ids=["f1"],
            fact_pack=FACT_PACK,
        )
        assert result.passed is True

    def test_grounded_percentage(self):
        result = verify_sentence(
            sentence_text="My project achieved 94% accuracy.",
            cited_fact_ids=["f3"],
            fact_pack=FACT_PACK,
        )
        assert result.passed is True


class TestVerifySop:
    def test_all_pass(self):
        sentences = [
            ("My GPA of 8.2 positions me well for this program.", ["f1"]),
            ("The DAAD Scholarship funding of 861 euros monthly is invaluable.", ["f2"]),
        ]
        results = verify_sop(sentences, FACT_PACK)
        assert all(r.passed for r in results)

    def test_one_fails(self):
        sentences = [
            ("My GPA of 8.2 is strong.", ["f1"]),
            ("I have a GPA of 9.9 which is perfect.", ["f4"]),  # 9.9 not in f4
        ]
        results = verify_sop(sentences, FACT_PACK)
        passing = [r for r in results if r.passed]
        failing = [r for r in results if not r.passed]
        assert len(passing) == 1
        assert len(failing) == 1
        assert failing[0].sentence_index == 1

    def test_sentence_indices_set(self):
        sentences = [("Sentence one with GPA 8.2.", ["f1"]), ("Sentence two.", [])]
        results = verify_sop(sentences, FACT_PACK)
        assert results[0].sentence_index == 0
        assert results[1].sentence_index == 1


class TestGetFailingIndices:
    def test_returns_only_failing(self):
        results = [
            ClaimVerificationResult(sentence_index=0, passed=True),
            ClaimVerificationResult(sentence_index=1, passed=False, failures=["bad"]),
            ClaimVerificationResult(sentence_index=2, passed=True),
        ]
        assert get_failing_sentence_indices(results) == [1]

    def test_all_pass(self):
        results = [ClaimVerificationResult(sentence_index=i, passed=True) for i in range(3)]
        assert get_failing_sentence_indices(results) == []
