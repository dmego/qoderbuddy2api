"""Bounded JSON and identifier validation for admin requests."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request

MAX_ADMIN_BODY_BYTES = 64 * 1024
LABEL_RE = re.compile(r"^[\w .-]{1,64}$")
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
FILTER_TEXT_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")
PROVIDERS = frozenset({"codebuddy", "qoder"})


async def json_object(
    request: Request,
    *,
    allow_empty: bool = False,
) -> dict[str, Any]:
    raw_length = request.headers.get("content-length")
    if raw_length and _too_large(raw_length):
        raise HTTPException(status_code=413, detail="request_body_too_large")
    raw = await request.body()
    if len(raw) > MAX_ADMIN_BODY_BYTES:
        raise HTTPException(status_code=413, detail="request_body_too_large")
    if not raw and allow_empty:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HTTPException(status_code=400, detail="invalid_json") from error
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="invalid_body")
    return parsed


def label(value: Any, *, default: str) -> str:
    selected = default if value is None else value
    if not isinstance(selected, str) or not LABEL_RE.fullmatch(selected):
        raise HTTPException(status_code=400, detail="invalid_label")
    return selected


def optional_account_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not ACCOUNT_ID_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail="invalid_account_id")
    return value


def required_string(body: dict[str, Any], *names: str, detail: str) -> str:
    value = next((body.get(name) for name in names if body.get(name)), None)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=detail)
    return value.strip()


def bounded_int(
    value: str | int | None,
    *,
    default: int,
    minimum: int = 1,
    maximum: int,
    detail: str = "invalid_limit",
) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=detail) from error
    if isinstance(value, bool) or not minimum <= parsed <= maximum:
        raise HTTPException(status_code=400, detail=detail)
    return parsed


def cursor_value(value: str | int | None, *, allow_zero: bool = False) -> int | None:
    if value is None:
        return None
    minimum = 0 if allow_zero else 1
    return bounded_int(
        value,
        default=minimum,
        minimum=minimum,
        maximum=2**63 - 1,
        detail="invalid_cursor",
    )


def choice_filter(
    value: str | None,
    allowed: Sequence[str] | set[str] | frozenset[str],
    *,
    detail: str,
) -> str | None:
    if value is not None and value not in allowed:
        raise HTTPException(status_code=400, detail=detail)
    return value


def provider_filter(value: str | None) -> str | None:
    return choice_filter(value, PROVIDERS, detail="invalid_provider")


def text_filter(value: str | None, *, detail: str) -> str | None:
    if value is not None and not FILTER_TEXT_RE.fullmatch(value):
        raise HTTPException(status_code=400, detail=detail)
    return value


def bool_filter(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.lower()
    if normalized not in {"true", "false"}:
        raise HTTPException(status_code=400, detail="invalid_enabled")
    return normalized == "true"


def time_range(
    started_after: str | None,
    started_before: str | None,
) -> tuple[str | None, str | None]:
    after = _timestamp(started_after, "invalid_started_after")
    before = _timestamp(started_before, "invalid_started_before")
    if after is not None and before is not None and after >= before:
        raise HTTPException(status_code=400, detail="invalid_time_range")
    return (
        after.isoformat() if after is not None else None,
        before.isoformat() if before is not None else None,
    )


def page_slice(items: Sequence[Any], cursor: int | None, limit: int) -> tuple[list[Any], int | None]:
    offset = cursor or 0
    page = list(items[offset : offset + limit])
    next_cursor = offset + len(page) if offset + len(page) < len(items) else None
    return page, next_cursor


def _timestamp(value: str | None, detail: str) -> datetime | None:
    if value is None:
        return None
    normalized = value.replace("Z", "+00:00")
    if len(normalized) >= 6 and normalized[-6] == " ":
        normalized = f"{normalized[:-6]}+{normalized[-5:]}"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=detail) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _too_large(value: str) -> bool:
    try:
        return int(value) > MAX_ADMIN_BODY_BYTES
    except ValueError:
        return False
