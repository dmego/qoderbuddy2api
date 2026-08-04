"""Qoder status, claim, and refresh response classification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from .base import (
    classify_http_error,
    extract_message,
    extract_request_id,
)
from .models import CheckInOutcome, CheckInResult, RefreshResult

_ALREADY_STATUS = frozenset(
    {"CLAIMED_TODAY", "ALREADY_CLAIMED", "ALREADY_CHECKED_IN", "CHECKED_IN"}
)
_CLAIMABLE_STATUS = frozenset({"CLAIMABLE", "NOT_CLAIMED", "AVAILABLE"})
_CLAIMED_RESULT = frozenset({"CLAIMED", "SUCCESS", "OK"})
_ALREADY_RESULT = frozenset(
    {"ALREADY_CLAIMED", "CLAIMED_TODAY", "ALREADY_CHECKED_IN"}
)
_STATUS_KEYS = ("status", "checkInStatus", "checkin_status", "state")
_RESULT_KEYS = ("result", "status", "claimResult")


@dataclass(frozen=True, slots=True)
class _ResponseContext:
    account_id: str
    status_code: int
    body: dict[str, Any] | None
    request_id: str | None
    reward: float | None
    reward_expires_at: str | None


def classify_status(
    *,
    status_code: int,
    body: dict[str, Any] | None,
    headers: httpx.Headers,
    account_id: str,
) -> CheckInResult:
    context = _context(
        status_code=status_code,
        body=body,
        headers=headers,
        account_id=account_id,
    )
    if not 200 <= status_code < 300:
        return _http_error(context)
    status = _extract_value(body, _STATUS_KEYS)
    normalized = status.upper() if status else ""
    if normalized in _ALREADY_STATUS:
        return _result(
            CheckInOutcome.ALREADY_CHECKED_IN,
            context,
            raw_status=status,
        )
    if normalized in _CLAIMABLE_STATUS:
        return _result(CheckInOutcome.SKIPPED, context, raw_status=status)
    message = extract_message(body)
    if normalized == "DISABLED":
        message = message or "Qoder daily check-in is disabled"
    return _result(
        CheckInOutcome.FAILED,
        context,
        raw_status=status,
        message=message or "unrecognized check-in status",
    )


def classify_claim(
    *,
    status_code: int,
    body: dict[str, Any] | None,
    headers: httpx.Headers,
    account_id: str,
) -> CheckInResult:
    context = _context(
        status_code=status_code,
        body=body,
        headers=headers,
        account_id=account_id,
    )
    result = _extract_value(body, _RESULT_KEYS)
    normalized = result.upper() if result else ""
    if status_code in {401, 403}:
        return _http_error(context)
    if normalized in _ALREADY_RESULT:
        return _result(
            CheckInOutcome.ALREADY_CHECKED_IN,
            context,
            raw_status=result,
        )
    if 200 <= status_code < 300 and normalized in _CLAIMED_RESULT:
        return _result(CheckInOutcome.CLAIMED, context, raw_status=result)
    if 200 <= status_code < 300:
        return _result(
            CheckInOutcome.FAILED,
            context,
            raw_status=result,
            message=extract_message(body) or "unrecognized check-in result",
        )
    return _http_error(context)


def classify_refresh(
    *,
    status_code: int,
    body: dict[str, Any] | None,
) -> RefreshResult:
    if not 200 <= status_code < 300:
        return RefreshResult(
            http_status=status_code,
            outcome=_refresh_error_outcome(status_code),
            message=extract_message(body) or f"http {status_code}",
        )
    access = _secret_value(body, ("device_token", "token", "access_token"))
    refresh = _secret_value(body, ("refresh_token",))
    if not access:
        return RefreshResult(
            http_status=status_code,
            outcome=CheckInOutcome.FAILED,
            message="refresh response missing access token",
        )
    return RefreshResult(
        access_token=access,
        refresh_token=refresh,
        http_status=status_code,
    )


def is_claimable(result: CheckInResult) -> bool:
    return bool(
        result.outcome == CheckInOutcome.SKIPPED
        and result.raw_status
        and result.raw_status.upper() in _CLAIMABLE_STATUS
    )


def _result(
    outcome: CheckInOutcome,
    context: _ResponseContext,
    *,
    raw_status: str | None,
    message: str | None = None,
) -> CheckInResult:
    return CheckInResult(
        outcome=outcome,
        provider="qoder",
        account_id=context.account_id,
        http_status=context.status_code,
        request_id=context.request_id,
        message=message if message is not None else extract_message(context.body),
        reward_credits=context.reward,
        reward_expires_at=context.reward_expires_at,
        raw_status=raw_status,
    )


def _context(
    *,
    status_code: int,
    body: dict[str, Any] | None,
    headers: httpx.Headers,
    account_id: str,
) -> _ResponseContext:
    return _ResponseContext(
        account_id=account_id,
        status_code=status_code,
        body=body,
        request_id=extract_request_id(body, headers),
        reward=_extract_reward(body),
        reward_expires_at=_extract_reward_expiry(body),
    )


def _http_error(context: _ResponseContext) -> CheckInResult:
    return classify_http_error(
        provider="qoder",
        account_id=context.account_id,
        status_code=context.status_code,
        body=context.body,
        request_id=context.request_id,
    )


def _extract_value(
    body: dict[str, Any] | None,
    keys: tuple[str, ...],
) -> str | None:
    if not body:
        return None
    for source in (body, body.get("data")):
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _extract_reward(body: dict[str, Any] | None) -> float | None:
    if not body:
        return None
    for source in (body, body.get("data")):
        if not isinstance(source, dict):
            continue
        for key in ("rewardCredits", "reward_credits"):
            value = source.get(key)
            if isinstance(value, (int, float)):
                return float(value)
    return None


def _extract_reward_expiry(body: dict[str, Any] | None) -> str | None:
    if not body:
        return None
    for source in (body, body.get("data")):
        if not isinstance(source, dict):
            continue
        for key in ("expiresAt", "expires_at", "rewardExpiresAt", "reward_expires_at"):
            value = source.get(key)
            normalized = _normalize_expiry(value)
            if normalized:
                return normalized
    return None


def _normalize_expiry(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _epoch_to_iso(value)
    text = str(value).strip()
    if text.isdigit():
        return _epoch_to_iso(int(text))
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).isoformat()


def _epoch_to_iso(value: int | float) -> str | None:
    seconds = value / 1000 if value > 10_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _secret_value(
    body: dict[str, Any] | None,
    keys: tuple[str, ...],
) -> str | None:
    value = _extract_value(body, keys)
    return value.strip() if value and value.strip() else None


def _refresh_error_outcome(status_code: int) -> CheckInOutcome:
    if status_code in {401, 403}:
        return CheckInOutcome.NEEDS_REAUTH
    if status_code == 429:
        return CheckInOutcome.RATE_LIMITED
    if status_code >= 500:
        return CheckInOutcome.TRANSIENT_ERROR
    return CheckInOutcome.FAILED
