"""Validation helpers for account and credential import routes."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from fastapi import HTTPException

from qb2api.checkin.executor_helpers import workbuddy_client
from qb2api.checkin.models import SUCCESS_OUTCOMES
from qb2api.checkin.qoder_credentials import derive_qoder_checkin as _derive_qoder_checkin
from qb2api.checkin.qoder_status import is_usable_checkin_result
from qb2api.config import Settings
from qb2api.providers.qoder_auth import QoderSession

from .validation import LABEL_RE, label

logger = logging.getLogger("qb2api.admin.import_support")


async def require_codebuddy_account(
    state: Any,
    account_id: str | None,
    *,
    required: bool = False,
) -> None:
    if account_id is None:
        if required:
            raise HTTPException(status_code=400, detail="account_id_required")
        return
    if state.account_registry.is_env_account("codebuddy", account_id):
        raise HTTPException(status_code=400, detail="cannot_reauthorize_env_account")
    accounts = await state.account_repo.list_accounts("codebuddy")
    if not any(account["account_id"] == account_id for account in accounts):
        raise HTTPException(status_code=404, detail="account_not_found")


async def codebuddy_label(state: Any, account_id: str | None, value: Any) -> str:
    await require_codebuddy_account(state, account_id)
    if account_id is None:
        return label(value, default="codebuddy")
    accounts = await state.account_repo.list_accounts("codebuddy")
    account = next(item for item in accounts if item["account_id"] == account_id)
    return label(value, default=account["label"])


def intl_identity(token: str) -> tuple[str | None, str | None]:
    """Best-effort identity from a WorkBuddy intl Keycloak JWT.

    Returns ``(sub, label)``. ``sub`` is the stable upstream user id used to
    deduplicate logins; ``label`` is the first usable display identity
    (email, then login handle, then name) or None. Opaque or broken tokens
    yield ``(None, None)`` so callers keep their defaults.
    """
    try:
        encoded = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (IndexError, TypeError, ValueError):
        return None, None
    if not isinstance(claims, dict):
        return None, None
    sub = claims.get("sub")
    label = None
    for claim in ("email", "preferred_username", "name"):
        value = claims.get(claim)
        if isinstance(value, str):
            candidate = value.strip()
            if candidate and LABEL_RE.fullmatch(candidate) and len(candidate) <= 64:
                label = candidate
                break
    sub_text = sub.strip() if isinstance(sub, str) else None
    return sub_text or None, label


def orcaterm_identity(token: str) -> tuple[str | None, str | None]:
    """Best-effort identity from an OrcaTerm desktop OAuth JWT.

    Returns ``(userId, label)``. ``userId`` is the stable upstream account id
    (numeric string) used to deduplicate re-imports; ``label`` is the login
    name when the token carries one. Opaque or broken tokens yield
    ``(None, None)`` so callers keep their defaults.
    """
    try:
        encoded = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (IndexError, TypeError, ValueError):
        return None, None
    if not isinstance(claims, dict):
        return None, None
    user_id = claims.get("userId")
    if isinstance(user_id, int):
        sub = str(user_id)
    elif isinstance(user_id, str):
        sub = user_id.strip()
    else:
        sub = ""
    label_value = None
    for claim in ("nickName", "name", "preferred_username"):
        value = claims.get(claim)
        if isinstance(value, str):
            candidate = value.strip()
            if candidate and LABEL_RE.fullmatch(candidate) and len(candidate) <= 64:
                label_value = candidate
                break
    return sub or None, label_value


def workbuddy_input(body: dict[str, Any]) -> tuple[str, str | None, str | None]:
    mode = body.get("mode")
    access_token = _optional_secret(body, "access_token", "bearer", "token")
    cookie = _optional_secret(body, "cookie")
    selected = mode or (
        "bearer_cookie" if access_token and cookie else "cookie" if cookie else "bearer"
    )
    if selected not in {"bearer", "cookie", "bearer_cookie"}:
        raise HTTPException(status_code=400, detail="invalid_checkin_auth_mode")
    if selected in {"bearer", "bearer_cookie"} and not access_token:
        raise HTTPException(status_code=400, detail="access_token_required")
    if selected in {"cookie", "bearer_cookie"} and not cookie:
        raise HTTPException(status_code=400, detail="cookie_required")
    return selected, access_token, cookie


async def verify_workbuddy(
    state: Any,
    account_id: str,
    mode: str,
    *,
    access_token: str | None,
    cookie: str | None,
) -> Any:
    factory = getattr(state, "workbuddy_client_factory", None)
    client = factory() if factory is not None else workbuddy_client(state.settings)
    try:
        return await client.checkin(
            account_id=account_id,
            auth_mode=mode,
            access_token=access_token,
            cookie=cookie,
        )
    finally:
        await client.close()


def _optional_secret(body: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(status_code=400, detail=f"{key}_required")
        return value.strip()
    return None


async def derive_qoder_checkin(
    settings: Settings,
    pat: str,
) -> tuple[str, str] | None:
    del settings
    return await _derive_qoder_checkin(pat, session_factory=QoderSession)


async def verify_codebuddy_checkin(
    state: Any,
    access_token: str,
) -> bool:
    """Use only an explicitly configured status probe for automatic verification.

    Import must never fall through to the daily claim endpoint. When no reliable
    read-only status endpoint is configured, keep the check-in purpose unverified
    and let the explicit manual flow request confirmation from the administrator.
    """
    settings = state.settings
    if not (
        settings.codebuddy_checkin_enabled
        and settings.codebuddy_checkin_status_method
    ):
        return False
    factory = getattr(state, "workbuddy_client_factory", None)
    client = factory() if factory is not None else workbuddy_client(settings)
    try:
        result = await client.status(
            auth_mode="bearer",
            access_token=access_token,
        )
    except Exception:
        return False
    finally:
        await client.close()
    return result.outcome in SUCCESS_OUTCOMES


async def verify_qoder_checkin(
    state: Any,
    account_id: str,
    access_token: str,
) -> bool:
    """Verify a derived Qoder token before marking its check-in purpose active.

    Accepts any non-failing status: an already-checked-in signal (CLAIMED_TODAY,
    ALREADY_CLAIMED, ALREADY_CHECKED_IN, CHECKED_IN…), a claimable signal, or a
    just-claimed result. Only explicit auth/transport failures reject the token.
    """
    try:
        result = await state.checkin_service.qoder_client.status(
            access_token=access_token,
            account_id=account_id,
        )
    except Exception:
        return False
    return is_usable_checkin_result(result)
