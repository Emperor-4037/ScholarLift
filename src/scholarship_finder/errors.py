"""Normalized Error Taxonomy for the Scholarship Finder.

Every adapter MUST translate vendor-specific failures into this hierarchy.
SDK exceptions and vendor types must never cross a port boundary.

Error classes map to the architecture §2.3 taxonomy:
  RateLimited · QuotaExhausted · Timeout · Transient · SchemaViolation
  ContextTooLong · ContentBlocked · ModelUnavailable · AuthError
  NotFound · Blocked · Unknown
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


# ── Base ──────────────────────────────────────────────────────────────────────

class ScholarshipFinderError(Exception):
    """Root of all application-defined exceptions."""


class DomainError(ScholarshipFinderError):
    """Violations of domain invariants (pure-logic errors)."""


# ── Adapter / Port Errors ─────────────────────────────────────────────────────

class RateLimitScope(str, Enum):
    MINUTE = "minute"
    DAY = "day"


class AdapterError(ScholarshipFinderError):
    """Base for all errors raised by port adapters.

    Attributes:
        provider: Which vendor or adapter raised this error.
        message: Human-readable explanation.
    """

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(message)
        self.provider = provider
        self.message = message

    @property
    def retry_eligible(self) -> bool:
        """Whether this error class warrants an automatic retry."""
        return False

    def __repr__(self) -> str:
        return f"{type(self).__name__}(provider={self.provider!r}, message={self.message!r})"


class RateLimitedError(AdapterError):
    """429 / RESOURCE_EXHAUSTED with a per-minute scope.

    The Quota Governor inspects `retry_after`: if ≤8 s, wait and retry;
    if >8 s, fail over to the next chain entry.
    """

    def __init__(
        self,
        provider: str,
        message: str,
        scope: RateLimitScope = RateLimitScope.MINUTE,
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(provider, message)
        self.scope = scope
        self.retry_after = retry_after  # seconds; 0 = unknown

    @property
    def retry_eligible(self) -> bool:
        return self.scope == RateLimitScope.MINUTE and 0 < self.retry_after <= 8.0


class QuotaExhaustedError(AdapterError):
    """Daily quota (RPD) exhausted or limit=0.

    The circuit breaker transitions to KNOWN_EXHAUSTED; no retry.
    """

    @property
    def retry_eligible(self) -> bool:
        return False


class TimeoutError(AdapterError):  # noqa: A001
    """Network or inference timeout on a single attempt.

    Fetch timeouts escalate to the next tier; LLM timeouts fail over.
    Never same-tier retry for fetches.
    """

    @property
    def retry_eligible(self) -> bool:
        return False   # handled by tier escalation, not retry


class TransientError(AdapterError):
    """Transient network or server error (5xx, connection reset, DNS).

    Eligible for up to 2 retries with jittered exponential backoff (~0.5 s, ~2 s).
    """

    @property
    def retry_eligible(self) -> bool:
        return True


class SchemaViolationError(AdapterError):
    """The provider returned output that fails Pydantic validation.

    One repair attempt is made (re-submit with the validation error);
    after that, the field is marked UNVERIFIED.
    """

    def __init__(self, provider: str, message: str, raw_output: str = "") -> None:
        super().__init__(provider, message)
        self.raw_output = raw_output

    @property
    def retry_eligible(self) -> bool:
        return True   # one repair attempt allowed


class ContextTooLongError(AdapterError):
    """Input exceeds the model's context window.

    The caller must truncate or split the input before retrying.
    """

    def __init__(self, provider: str, message: str, token_count: int = 0) -> None:
        super().__init__(provider, message)
        self.token_count = token_count

    @property
    def retry_eligible(self) -> bool:
        return False


class ContentBlockedError(AdapterError):
    """Provider safety filter blocked the request or response.

    Falls through to the next chain entry (different provider may handle it).
    """

    @property
    def retry_eligible(self) -> bool:
        return False


class ModelUnavailableError(AdapterError):
    """Model is retired, 404, or not enabled on this account.

    The circuit breaker transitions to KNOWN_EXHAUSTED permanently (no reset).
    The model is demoted in the chain.
    """

    @property
    def retry_eligible(self) -> bool:
        return False


class AuthError(AdapterError):
    """Invalid or missing credentials.

    Fail fast; open circuit immediately. Never retry.
    """

    @property
    def retry_eligible(self) -> bool:
        return False


class NotFoundError(AdapterError):
    """HTTP 404 or DNS resolution failure for a URL.

    One retry at most; then mark as failed with this class.
    """

    @property
    def retry_eligible(self) -> bool:
        return True   # one retry allowed (DNS flap, redirect)


class BlockedError(AdapterError):
    """Anti-bot, captcha, or 403 from a web server.

    Escalate fetch tier; trip per-domain breaker for the rest of the run.
    """

    @property
    def retry_eligible(self) -> bool:
        return False


class UnknownAdapterError(AdapterError):
    """Catch-all for unexpected vendor failures.

    Logged and treated as Transient for retry purposes.
    """

    def __init__(self, provider: str, message: str, original: Optional[Exception] = None) -> None:
        super().__init__(provider, message)
        self.original = original

    @property
    def retry_eligible(self) -> bool:
        return True
