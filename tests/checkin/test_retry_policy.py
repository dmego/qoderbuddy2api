"""Bounded check-in retry policy contracts."""

from __future__ import annotations

import pytest

from qb2api.checkin.models import CheckInOutcome, CheckInResult
from qb2api.checkin.retry import run_with_retry


def _result(outcome: CheckInOutcome, *, status: int | None = None) -> CheckInResult:
    return CheckInResult(
        outcome=outcome,
        provider="codebuddy",
        account_id="cb-main",
        http_status=status,
    )


@pytest.mark.asyncio
async def test_transient_result_retries_until_success() -> None:
    results = [
        _result(CheckInOutcome.TRANSIENT_ERROR, status=503),
        _result(CheckInOutcome.CLAIMED, status=200),
    ]
    delays: list[float] = []

    async def operation() -> CheckInResult:
        return results.pop(0)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    result, attempts = await run_with_retry(
        operation,
        retry_limit=2,
        sleep=sleep,
        jitter=lambda _attempt: 0.25,
    )

    assert result.outcome == CheckInOutcome.CLAIMED
    assert attempts == 2
    assert delays == [0.25]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        CheckInOutcome.NEEDS_REAUTH,
        CheckInOutcome.AUTH_FAILED,
        CheckInOutcome.FAILED,
        CheckInOutcome.ALREADY_CHECKED_IN,
    ],
)
async def test_terminal_and_business_failures_do_not_retry(outcome) -> None:
    calls = 0

    async def operation() -> CheckInResult:
        nonlocal calls
        calls += 1
        return _result(outcome, status=400)

    result, attempts = await run_with_retry(operation, retry_limit=3)

    assert result.outcome == outcome
    assert attempts == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_limit_zero_runs_once() -> None:
    calls = 0

    async def operation() -> CheckInResult:
        nonlocal calls
        calls += 1
        return _result(CheckInOutcome.RATE_LIMITED, status=429)

    result, attempts = await run_with_retry(operation, retry_limit=0)

    assert result.outcome == CheckInOutcome.RATE_LIMITED
    assert attempts == 1
    assert calls == 1
