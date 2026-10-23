"""PII scrubber and per-run PII Vault.

Restricted PII (name, email, phone, IDs) is scrubbed from all text
BEFORE it reaches any LLM, search query, or log.
Identity data is stored only in the per-run PIIVault and rehydrated
locally when rendering the final SOP.
Auto-purge after TTL (default 15 minutes) or on explicit user trigger.

Architecture reference: §1.5, §2.4, §3.6, OPEN-6.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Compiled PII regex patterns
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Email — handles user+tag@sub.domain.co.uk
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
    re.IGNORECASE,
)

# Phone — international formats: +91-9876543210, (123) 456-7890, +1 800 555 1234, etc.
_PHONE_RE = re.compile(
    r"""
    (?:
        \+\d{1,3}[\s\-.]?          # country code +91, +1, etc.
    )?
    (?:
        \(?\d{3}\)?[\s\-.]?        # area code with optional parens
        \d{3,4}[\s\-.]?            # exchange
        \d{4,6}                    # number
        |
        \d{5,6}[\s\-.]?\d{4,5}    # compact formats
    )
    """,
    re.VERBOSE,
)

# Aadhaar-like: 12 consecutive digits (sometimes space/hyphen separated as 4-4-4)
_AADHAAR_RE = re.compile(r"\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b")

# Passport-like: letter(s) + 7-8 digits
_PASSPORT_RE = re.compile(r"\b[A-Z]{1,2}\d{7,8}\b")

# SSN-like: NNN-NN-NNNN
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# GitHub URLs: github.com/username or github.com/org/username
# Matches both single-segment (user profile) and double-segment (org repository) URLs
_GITHUB_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/[\w\-]+(?:/[\w\-]+)?",
    re.IGNORECASE,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PIIVault
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PIIVault:
    """Per-run in-memory store for restricted identity fields.

    Placeholders like ``[CANDIDATE_NAME]`` map back to originals.
    The vault auto-expires after ``ttl_seconds`` and can be manually purged.
    """

    def __init__(self, ttl_seconds: int = 900) -> None:
        self.vault_id: str = str(uuid.uuid4())
        self.ttl_seconds: int = ttl_seconds
        self._store: dict[str, str] = {}       # placeholder → original
        self._created_at: datetime = datetime.now(tz=timezone.utc)

    @property
    def is_expired(self) -> bool:
        elapsed = (datetime.now(tz=timezone.utc) - self._created_at).total_seconds()
        return elapsed >= self.ttl_seconds

    def store(self, field_name: str, value: str) -> str:
        """Store *value* under *field_name* and return its placeholder.

        Example: store("name", "Rahul Sharma") → "[CANDIDATE_NAME]"
        """
        placeholder = f"[CANDIDATE_{field_name.upper()}]"
        self._store[placeholder] = value
        return placeholder

    def retrieve(self, placeholder: str) -> str | None:
        """Return the original value or None if expired or not found."""
        if self.is_expired:
            return None
        return self._store.get(placeholder)

    def rehydrate(self, text: str) -> str:
        """Replace all placeholders in *text* with their stored originals.

        Safe to call even after expiry — returns text unchanged if expired.
        """
        if self.is_expired:
            return text
        result = text
        for placeholder, original in self._store.items():
            result = result.replace(placeholder, original)
        return result

    def purge(self) -> None:
        """Immediately clear all stored identity data."""
        self._store.clear()

    def __del__(self) -> None:  # noqa: D105
        self.purge()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PIIScrubber
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PIIScrubber:
    """Scrub restricted PII from text and store originals in a PIIVault.

    Scrub order:
      1. Display name (case-insensitive, whole-word)
      2. Email addresses
      3. Phone numbers
      4. URLs containing the display name
      5. Aadhaar-like 12-digit IDs
      6. Passport-like IDs
      7. SSN-like IDs
    """

    def __init__(self, display_name: str | None = None) -> None:
        self.display_name = display_name
        # Pre-compile a case-insensitive whole-word pattern for the display name
        self._name_re: re.Pattern[str] | None = None
        if display_name and display_name.strip():
            escaped = re.escape(display_name.strip())
            self._name_re = re.compile(rf"\b{escaped}\b", re.IGNORECASE)

    def scrub(self, text: str, vault: PIIVault) -> str:
        """Scrub all PII from *text*, storing originals in *vault*.

        Args:
            text:   Input text (resume, transcript, etc.).
            vault:  Per-run PIIVault to store the originals.

        Returns:
            Scrubbed text with PII replaced by placeholders.
        """
        # 1. Display name
        if self._name_re and self.display_name:
            placeholder = vault.store("name", self.display_name)
            text = self._name_re.sub(placeholder, text)

        # 2. Emails (before phone to avoid partial matches)
        def _replace_email(m: re.Match[str]) -> str:
            original = m.group()
            vault.store("email", original)
            return "[REDACTED_EMAIL]"

        text = _EMAIL_RE.sub(_replace_email, text)

        # 3. Aadhaar-like 12-digit IDs — BEFORE phone regex (phone pattern is broader)
        text = _AADHAAR_RE.sub("[REDACTED_ID]", text)

        # 4. Passport-like IDs
        text = _PASSPORT_RE.sub("[REDACTED_ID]", text)

        # 5. SSN-like IDs
        text = _SSN_RE.sub("[REDACTED_ID]", text)

        # 6. Phone numbers (runs after IDs so Aadhaar digits are already masked)
        text = _PHONE_RE.sub("[REDACTED_PHONE]", text)

        # 7. URLs containing the display name
        if self.display_name:
            name_slug = re.escape(self.display_name.strip().replace(" ", "").lower())
            url_name_re = re.compile(
                rf"https?://[^\s]*{name_slug}[^\s]*",
                re.IGNORECASE,
            )
            text = url_name_re.sub("[REDACTED_URL]", text)

        return text


# ── Convenience function ──────────────────────────────────────────────────────

def scrub_text(text: str, display_name: str | None, vault: PIIVault) -> str:
    """Convenience wrapper around PIIScrubber.scrub."""
    return PIIScrubber(display_name=display_name).scrub(text, vault)
