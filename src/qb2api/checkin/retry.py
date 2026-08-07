"""Bounded retry policy for transient check-in outcomes."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable

from .models import CheckInOutcome, CheckInResult

Operation = Callable[[], Awaitable[CheckInResult]]
Sleeper = Callable[[float], Awaitable[None]]
Jitter = Callable[[int], float]

_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
_MAX_RETRY_AFTER_SECONDS = 30.0


async def run_with_retry(
    operation: Operation,
    *,
    retry_limit: int,
    sleep: Sleeper = asyncio.sleep,
    jitter: Jitter | None = None,
) -> tuple[CheckInResult, int]:
    attempts = 0
    while True:
        attempts += 1
        result = await operation()
        if attempts > max(0, retry_limit) or not should_retry(result):
            return result, attempts
        delay = _retry_after(result)
        if delay is None:
            delay = (jitter or _default_jitter)(attempts)
        await sleep(delay)


def should_retry(result: CheckInResult) -> bool:
    if result.outcome == CheckInOutcome.RATE_LIMITED:
        return result.http_status in (None, 429)
    if result.outcome != CheckInOutcome.TRANSIENT_ERROR:
        return False
    return result.http_status is None or result.http_status in _RETRYABLE_STATUS


def _retry_after(result: CheckInResult) -> float | None:
    value = result.extra.get("retry_after")
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return min(_MAX_RETRY_AFTER_SECONDS, max(0.0, parsed))


def _default_jitter(attempt: int) -> float:
    ceiling_ms = min(8000, 500 * (2 ** max(0, attempt - 1)))
    return secrets.randbelow(ceiling_ms + 1) / 1000.0
