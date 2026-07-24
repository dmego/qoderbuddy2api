"""Bounded JSON and identifier validation for admin requests."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import HTTPException, Request

MAX_ADMIN_BODY_BYTES = 64 * 1024
LABEL_RE = re.compile(r"^[\w .-]{1,64}$")
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


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


def _too_large(value: str) -> bool:
    try:
        return int(value) > MAX_ADMIN_BODY_BYTES
    except ValueError:
        return False
