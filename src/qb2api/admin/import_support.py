"""Validation helpers for account and credential import routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from qb2api.checkin.executor_helpers import qoder_client, workbuddy_client
from qb2api.checkin.models import SUCCESS_OUTCOMES, CheckInOutcome
from qb2api.config import Settings
from qb2api.providers.qoder_auth import QoderError, QoderSession

from .validation import label

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
    """用 PAT 认证后派生签到凭据 (access_token, refresh_token)。

    链路: PAT -> jobToken 响应带 refreshToken -> deviceToken/refresh -> access_token。
    返回 None 表示派生失败，不阻塞 PAT 导入。
    """
    refresh_token = await _qoder_refresh_token_from_pat(pat)
    if not refresh_token:
        return None
    return await _qoder_access_tokens_from_refresh(settings, refresh_token)


async def _qoder_refresh_token_from_pat(pat: str) -> str | None:
    """Authenticate the PAT and return the opaque refresh token if supplied."""
    session = QoderSession(pat)
    try:
        await session.authenticate()
    except QoderError as error:
        logger.warning(
            "qoder checkin derive: authenticate failed (http=%s)",
            error.status_code,
        )
        return None
    except Exception as error:
        logger.warning(
            "qoder checkin derive: authenticate unexpected error: %s",
            type(error).__name__,
        )
        return None
    finally:
        await session.close()

    refresh_token = session.refresh_token
    if not refresh_token:
        logger.warning(
            "qoder checkin derive: no refreshToken in jobToken response "
            "(security_oauth_token present: %s)",
            bool(session.security_oauth_token),
        )
        return None
    return refresh_token


async def _qoder_access_tokens_from_refresh(
    settings: Settings,
    refresh_token: str,
) -> tuple[str, str] | None:
    """Refresh the device token without exposing upstream response text."""
    client = qoder_client(settings)
    try:
        result = await client.refresh(refresh_token=refresh_token)
    except Exception as error:
        logger.warning(
            "qoder checkin derive: refresh request error: %s",
            type(error).__name__,
        )
        return None
    finally:
        await client.close()

    if not result.ok or not result.access_token:
        logger.warning(
            "qoder checkin derive: refresh failed (outcome=%s, http=%s)",
            result.outcome,
            result.http_status,
        )
        return None

    # 上游可能轮换 refresh_token，取响应中的新值
    final_refresh = result.refresh_token or refresh_token
    return result.access_token, final_refresh


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
    """Verify a derived Qoder token before marking its check-in purpose active."""
    try:
        result = await state.checkin_service.qoder_client.status(
            access_token=access_token,
            account_id=account_id,
        )
    except Exception:
        return False
    return result.outcome in {
        CheckInOutcome.ALREADY_CHECKED_IN,
        CheckInOutcome.SKIPPED,
    }
