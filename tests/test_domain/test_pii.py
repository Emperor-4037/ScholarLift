"""Tests for PII scrubber and vault."""
from __future__ import annotations

import time

import pytest

from scholarship_finder.domain.pii import PIIScrubber, PIIVault, scrub_text


class TestPIIVault:
    def test_store_and_retrieve(self, sample_vault):
        placeholder = sample_vault.store("name", "Rahul Sharma")
        assert placeholder == "[CANDIDATE_NAME]"
        assert sample_vault.retrieve("[CANDIDATE_NAME]") == "Rahul Sharma"

    def test_rehydrate_single(self, sample_vault):
        sample_vault.store("name", "Rahul Sharma")
        text = "Dear [CANDIDATE_NAME], your application..."
        result = sample_vault.rehydrate(text)
        assert result == "Dear Rahul Sharma, your application..."

    def test_rehydrate_multiple(self, sample_vault):
        sample_vault.store("name", "Rahul")
        sample_vault.store("email", "r@example.com")
        text = "[CANDIDATE_NAME] ([REDACTED_EMAIL])"
        # Only [CANDIDATE_NAME] is in vault; [REDACTED_EMAIL] is not stored
        result = sample_vault.rehydrate(text)
        assert "Rahul" in result

    def test_expired_vault_returns_none(self):
        vault = PIIVault(ttl_seconds=0)
        vault.store("name", "Test User")
        time.sleep(0.01)
        assert vault.is_expired is True
        assert vault.retrieve("[CANDIDATE_NAME]") is None

    def test_expired_vault_rehydrate_unchanged(self):
        vault = PIIVault(ttl_seconds=0)
        vault.store("name", "Test User")
        time.sleep(0.01)
        text = "Hello [CANDIDATE_NAME]"
        assert vault.rehydrate(text) == text

    def test_purge_clears_data(self, sample_vault):
        sample_vault.store("name", "Rahul Sharma")
        sample_vault.purge()
        assert sample_vault.retrieve("[CANDIDATE_NAME]") is None

    def test_vault_has_unique_id(self):
        v1 = PIIVault()
        v2 = PIIVault()
        assert v1.vault_id != v2.vault_id


class TestPIIScrubber:
    def test_scrubs_display_name(self, sample_vault):
        scrubber = PIIScrubber(display_name="Rahul Sharma")
        result = scrubber.scrub("My name is Rahul Sharma and I applied.", sample_vault)
        assert "Rahul Sharma" not in result
        assert "[CANDIDATE_NAME]" in result

    def test_scrubs_email(self, sample_vault):
        scrubber = PIIScrubber()
        result = scrubber.scrub("Contact me at user@example.com please.", sample_vault)
        assert "user@example.com" not in result
        assert "[REDACTED_EMAIL]" in result

    def test_scrubs_email_with_tag(self, sample_vault):
        scrubber = PIIScrubber()
        result = scrubber.scrub("rahul+jobs@university.ac.in", sample_vault)
        assert "@university.ac.in" not in result
        assert "[REDACTED_EMAIL]" in result

    def test_scrubs_phone_international(self, sample_vault):
        scrubber = PIIScrubber()
        result = scrubber.scrub("Call me at +91-9876543210", sample_vault)
        assert "9876543210" not in result
        assert "[REDACTED_PHONE]" in result

    def test_scrubs_aadhaar(self, sample_vault):
        scrubber = PIIScrubber()
        result = scrubber.scrub("Aadhaar: 1234 5678 9012", sample_vault)
        assert "1234 5678 9012" not in result
        assert "[REDACTED_ID]" in result

    def test_scrubs_passport(self, sample_vault):
        scrubber = PIIScrubber()
        result = scrubber.scrub("Passport: A1234567", sample_vault)
        assert "A1234567" not in result
        assert "[REDACTED_ID]" in result

    def test_case_insensitive_name(self, sample_vault):
        scrubber = PIIScrubber(display_name="John Doe")
        result = scrubber.scrub("JOHN DOE applied. john doe is eligible.", sample_vault)
        assert "JOHN DOE" not in result
        assert "john doe" not in result

    def test_no_false_positive_on_normal_text(self, sample_vault):
        scrubber = PIIScrubber()
        text = "The minimum GPA is 3.0 and deadline is January 15, 2025."
        result = scrubber.scrub(text, sample_vault)
        assert result == text  # nothing should be scrubbed


class TestScrubTextConvenience:
    def test_convenience_function(self, sample_vault):
        result = scrub_text("My name is Rahul Sharma", "Rahul Sharma", sample_vault)
        assert "Rahul Sharma" not in result
        assert "[CANDIDATE_NAME]" in result
