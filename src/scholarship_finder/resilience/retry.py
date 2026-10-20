"""Retry / repair decorator for adapter calls.

Retry policy per error class (architecture §3.4):
  RateLimited(MINUTE, retry_after ≤8s): wait hint + jitter, up to 2 retries
  Transient:                            jittered exponential backoff (~0.5s, ~2s), 2 retries
  NotFound:                             1 retry
  SchemaViolation:                      1 repair (re-submit with validation error)
  QuotaExhausted, ModelUnavailable:     never retry (fail over immediately)
  AuthError:                            fail fast
  Timeout (fetch):                      no same-tier retry (escalate tier instead)
"""
from __future__ import annotations

import asyncio
import functools
import random
from typing import Any, Callable, TypeVar

from scholarship_finder.errors import (
    AdapterError,
    AuthError,
    ModelUnavailableError,
    NotFoundError,
    QuotaExhaustedError,
    RateLimitedError,
    RateLimitScope,
    SchemaViolationError,
    TimeoutError as SFTimeoutError,
    TransientError,
)

T = TypeVar("T")

_JITTER_RANGE = 0.1   # ±10% jitter on backoff waits


def _jitter(base: float) -> float:
    return base * (1.0 + random.uniform(-_JITTER_RANGE, _JITTER_RANGE))


async def with_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_retries: int = 2,
    **kwargs: Any,
) -> Any:
    """Execute *fn* with automatic retry logic based on error taxonomy.

    Args:
        fn:          Async callable to execute.
        max_retries: Maximum number of retry attempts.
        *args/**kwargs: Forwarded to *fn*.

    Returns:
        The result of *fn* on success.

    Raises:
        The last AdapterError if all retries are exhausted.
        Immediately re-raises non-retryable errors.
    """
    last_error: AdapterError | None = None

    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)

        except (QuotaExhaustedError, ModelUnavailableError, AuthError, SFTimeoutError):
            raise   # Never retry these; caller handles fail-over

        except RateLimitedError as e:
            if e.scope == RateLimitScope.DAY:
                raise   # Day-scope = quota exhausted; no retry
            if attempt >= max_retries:
                raise
            wait = _jitter(e.retry_after) if e.retry_after > 0 else _jitter(1.0)
            await asyncio.sleep(wait)
            last_error = e

        except NotFoundError as e:
            if attempt >= 1:  # Only one retry for 404/DNS
                raise
            await asyncio.sleep(_jitter(0.5))
            last_error = e

        except SchemaViolationError as e:
            if attempt >= 1:  # One repair attempt
                raise
            # Schema repair: caller must handle; just re-raise after one pass
            raise

        except TransientError as e:
            if attempt >= max_retries:
                raise
            # Jittered exponential: 0.5s, 2s
            wait = _jitter(0.5 * (2 ** attempt))
            await asyncio.sleep(wait)
            last_error = e

        except AdapterError as e:
            if e.retry_eligible and attempt < max_retries:
                await asyncio.sleep(_jitter(0.5))
                last_error = e
            else:
                raise

    raise last_error  # type: ignore[misc]


def retryable(max_retries: int = 2) -> Callable:
    """Decorator version of with_retry for async adapter methods."""
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await with_retry(fn, *args, max_retries=max_retries, **kwargs)
        return wrapper
    return decorator
