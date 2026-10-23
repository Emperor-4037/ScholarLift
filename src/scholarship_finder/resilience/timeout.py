"""Timeout decorator — innermost enforcement layer of the 6-layer chain.

Wraps an async callable with ``asyncio.wait_for``, mapping
``asyncio.TimeoutError`` → :class:`~scholarship_finder.errors.SFTimeoutError`.

Usage
-----
    from scholarship_finder.resilience.timeout import with_timeout

    result = await with_timeout(my_coroutine(), seconds=10.0, label="llm_call")

    # Or as a decorator factory:
    wrapped = timeout_wrap(fn, seconds=30.0, label="fetch")
    result = await wrapped(url)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Coroutine, TypeVar

from scholarship_finder.errors import TimeoutError as SFTimeoutError

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


async def with_timeout(
    coro: Coroutine[Any, Any, _T] | Awaitable[_T],
    *,
    seconds: float,
    label: str = "operation",
) -> _T:
    """Await *coro* with a hard deadline.

    Parameters
    ----------
    coro:
        Coroutine or awaitable to execute.
    seconds:
        Maximum wall-clock seconds to wait.
    label:
        Human-readable name for logging / error messages.

    Raises
    ------
    SFTimeoutError
        If the coroutine does not complete within *seconds*.
    """
    if seconds <= 0:
        raise ValueError(f"timeout seconds must be > 0, got {seconds!r}")

    try:
        return await asyncio.wait_for(asyncio.ensure_future(coro), timeout=seconds)  # type: ignore[arg-type]
    except asyncio.TimeoutError as exc:
        logger.warning("Timeout after %.1fs: %s", seconds, label)
        raise SFTimeoutError(
            provider=label,
            message=f"{label} timed out after {seconds:.1f}s",
        ) from exc


def timeout_wrap(
    fn: Callable[..., Awaitable[_T]],
    *,
    seconds: float,
    label: str | None = None,
) -> Callable[..., Awaitable[_T]]:
    """Return a version of *fn* that always runs under a hard deadline.

    Parameters
    ----------
    fn:
        Async function to wrap.
    seconds:
        Maximum allowed seconds per call.
    label:
        Descriptive label for error messages; defaults to ``fn.__name__``.
    """
    _label = str(label or getattr(fn, "__name__", "unknown"))

    async def _wrapper(*args: Any, **kwargs: Any) -> _T:
        return await with_timeout(fn(*args, **kwargs), seconds=seconds, label=_label)

    _wrapper.__name__ = f"timeout_wrapped({_label})"
    _wrapper.__qualname__ = _wrapper.__name__
    return _wrapper  # type: ignore[return-value]
