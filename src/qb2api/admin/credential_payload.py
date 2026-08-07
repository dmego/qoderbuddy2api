"""Validation and construction of credential rotation payloads."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

_CREDENTIAL_MODES = frozenset(
    {"bearer", "cookie", "bearer_cookie", "pat", "access_refresh", "oauth"}
)


def rotation_payload(
    *,
    provider: str,
    purpose: str,
    current: dict[str, Any],
    body: dict[str, Any],
) -> tuple[str, dict[str, str], str]:
    mode = _rotation_mode(body, current=current)
    access = _credential_value(body, "token", "access_token", "pat")
    cookie = _credential_value(body, "cookie")
    _validate_rotation_material(mode, access=access, cookie=cookie)
    _validate_provider_mode(provider, purpose=purpose, mode=mode)
    payload = _credential_payload(mode, access=access, cookie=cookie, body=body)
    return mode, payload, access or cookie or ""


def expires_at(body: dict[str, Any], fallback: str | None) -> str | None:
    value = body.get("expires_at", fallback)
    if value is not None and not isinstance(value, str):
        raise HTTPException(status_code=400, detail="invalid_expires_at")
    return value


def expected_version(body: dict[str, Any]) -> int | None:
    value = body.get("credential_version")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HTTPException(status_code=400, detail="invalid_credential_version")
    return value


def _rotation_mode(body: dict[str, Any], *, current: dict[str, Any]) -> str:
    mode = body.get("mode", current.get("mode") or "bearer")
    if not isinstance(mode, str) or mode not in _CREDENTIAL_MODES:
        raise HTTPException(status_code=400, detail="invalid_credential_mode")
    return mode


def _validate_rotation_material(mode: str, *, access: str | None, cookie: str | None) -> None:
    if mode in {"bearer", "bearer_cookie", "access_refresh", "pat", "oauth"} and not access:
        raise HTTPException(status_code=400, detail="token_required")
    if mode in {"cookie", "bearer_cookie"} and not cookie:
        raise HTTPException(status_code=400, detail="cookie_required")


def _validate_provider_mode(provider: str, *, purpose: str, mode: str) -> None:
    if provider == "qoder" and purpose == "chat" and mode != "pat":
        raise HTTPException(status_code=400, detail="invalid_credential_mode")


def _credential_payload(
    mode: str,
    *,
    access: str | None,
    cookie: str | None,
    body: dict[str, Any],
) -> dict[str, str]:
    payload = {"pat" if mode == "pat" else "access_token": access} if access else {}
    if cookie:
        payload["cookie"] = cookie
    refresh = _credential_value(body, "refresh_token")
    if refresh:
        payload["refresh_token"] = refresh
    return payload


def _credential_value(body: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(status_code=400, detail=f"invalid_{key}")
        return value.strip()
    return None
