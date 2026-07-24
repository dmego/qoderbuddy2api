"""Validation helpers for account and credential import routes."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from qb2api.checkin.executor_helpers import workbuddy_client

from .validation import label


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
