"""Shared check-in client helpers and classifier (design §9 / §10)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .models import CheckInOutcome, CheckInResult

logger = logging.getLogger("qb2api.checkin")


def parse_json_body(text: str) -> dict[str, Any] | None:
    """Parse JSON object body; return None on empty/invalid."""
    if not text or not text.strip():
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def extract_business_code(body: dict[str, Any] | None) -> str | int | None:
    if not body:
        return None
    for key in ("code", "businessCode", "business_code", "errCode", "error_code"):
        if key in body and body[key] is not None:
            return body[key]
    return None


def extract_request_id(body: dict[str, Any] | None, headers: httpx.Headers | None = None) -> str | None:
    if body:
        for key in ("requestId", "request_id", "reqId"):
            val = body.get(key)
            if val:
                return str(val)
    if headers:
        for key in ("x-request-id", "x-requestid", "request-id"):
            val = headers.get(key)
            if val:
                return val
    return None


def extract_message(body: dict[str, Any] | None) -> str | None:
    if not body:
        return None
    for key in ("msg", "message", "error", "detail"):
        val = body.get(key)
        if isinstance(val, str) and val:
            return val[:200]  # redacted length cap; never log secrets
    return None


def classify_http_error(
    *,
    provider: str,
    account_id: str,
    status_code: int,
    body: dict[str, Any] | None,
    request_id: str | None = None,
) -> CheckInResult:
    """Map generic HTTP failures to CheckInOutcome (provider-specific overrides first)."""
    outcome, fallback_message = _http_failure(status_code)
    return _http_error_result(
        outcome=outcome,
        fallback_message=fallback_message,
        provider=provider,
        account_id=account_id,
        status_code=status_code,
        body=body,
        request_id=request_id,
    )


def _http_failure(status_code: int) -> tuple[CheckInOutcome, str]:
    if status_code in (401, 403):
        return CheckInOutcome.NEEDS_REAUTH, "authentication failed"
    if status_code == 429:
        return CheckInOutcome.RATE_LIMITED, "rate limited"
    if status_code >= 500:
        return CheckInOutcome.TRANSIENT_ERROR, "upstream server error"
    return CheckInOutcome.FAILED, f"http {status_code}"


def _http_error_result(
    *,
    outcome: CheckInOutcome,
    fallback_message: str,
    provider: str,
    account_id: str,
    status_code: int,
    body: dict[str, Any] | None,
    request_id: str | None,
) -> CheckInResult:
    return CheckInResult(
        outcome=outcome,
        provider=provider,
        account_id=account_id,
        http_status=status_code,
        business_code=extract_business_code(body),
        request_id=request_id or extract_request_id(body),
        message=extract_message(body) or fallback_message,
    )


def classify_transport_error(
    *,
    provider: str,
    account_id: str,
    exc: BaseException,
) -> CheckInResult:
    """Network / timeout → TRANSIENT_ERROR. Never include secret-bearing context."""
    name = type(exc).__name__
    return CheckInResult(
        outcome=CheckInOutcome.TRANSIENT_ERROR,
        provider=provider,
        account_id=account_id,
        message=f"transport error: {name}",
    )


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"
