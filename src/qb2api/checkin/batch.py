"""Check-in batch records and secret-free response mapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import SUCCESS_OUTCOMES, CheckInOutcome, CheckInResult

SUCCESS_VALUES = {outcome.value for outcome in SUCCESS_OUTCOMES}


@dataclass(slots=True)
class CheckinTarget:
    provider: str
    account_id: str


@dataclass
class CheckinBatchResult:
    run_id: str
    local_date: str
    timezone: str
    status: str
    results: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class RunContext:
    run_id: str
    local_date: str
    timezone: str
    results: list[dict[str, Any]] = field(default_factory=list)


def redact_result(result: CheckInResult) -> dict[str, Any]:
    return {
        "provider": result.provider,
        "account_id": result.account_id,
        "outcome": result.outcome.value,
        "http_status": result.http_status,
        "business_code": result.business_code,
        "request_id": result.request_id,
        "message": result.message,
        "reward_credits": result.reward_credits,
    }


def daily_state_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": row["provider"],
        "account_id": row["account_id"],
        "terminal_outcome": row.get("terminal_outcome"),
        "last_run_id": row.get("last_run_id"),
        "updated_at": row.get("updated_at"),
    }


def batch_view(batch: CheckinBatchResult | None) -> dict[str, Any] | None:
    if batch is None:
        return None
    return {
        "run_id": batch.run_id,
        "status": batch.status,
        "local_date": batch.local_date,
        "results": batch.results,
    }


def skipped_terminal(target: CheckinTarget) -> CheckInResult:
    return CheckInResult(
        outcome=CheckInOutcome.SKIPPED,
        provider=target.provider,
        account_id=target.account_id,
        message="already terminal today",
    )


def isolated_failure(target: CheckinTarget, error: Exception) -> CheckInResult:
    return CheckInResult(
        outcome=CheckInOutcome.FAILED,
        provider=target.provider,
        account_id=target.account_id,
        message=f"isolated failure: {type(error).__name__}",
    )
