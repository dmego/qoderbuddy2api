"""WorkBuddy international account login, import, and verification routes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from qb2api.accounts.imports import persist_workbuddy_intl_account
from qb2api.auth.flows import FlowBusyError
from qb2api.auth.workbuddy_intl import WorkBuddyIntlAuthError

from .dependencies import admin_state, require_admin
from .mutation_audit import refresh_after_mutation
from .validation import json_object, label, optional_account_id, required_string
from .views import account_view_dict, find_account_view

logger = logging.getLogger("qb2api.admin.workbuddy_intl")

router = APIRouter()

PROVIDER = "workbuddy_intl"


@router.post("/auth/workbuddy-intl/start")
async def workbuddy_intl_oauth_start(request: Request) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    if not state.settings.workbuddy_intl_oauth_enabled:
        raise HTTPException(status_code=400, detail="oauth_disabled")
    body = await json_object(request, allow_empty=True)
    account_id = optional_account_id(body.get("account_id"))
    await _require_intl_account(state, account_id)
    selected_label = label(body.get("label"), default="workbuddy-intl")
    try:
        started = await state.workbuddy_intl_oauth.start()
    except WorkBuddyIntlAuthError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    flow = state.oauth_flows.create(
        label=selected_label,
        auth_state=started.auth_state,
        auth_url=started.auth_url,
        account_id=account_id,
    )
    return {
        "flow_id": flow.flow_id,
        "auth_url": flow.auth_url,
        "expires_at": datetime.fromtimestamp(flow.expires_at, tz=UTC)
        .replace(microsecond=0)
        .isoformat(),
        "label": flow.label,
        "account_id": flow.account_id,
    }


@router.post("/auth/workbuddy-intl/poll")
async def workbuddy_intl_oauth_poll(request: Request) -> Any:
    await require_admin(request)
    state = admin_state(request)
    body = await json_object(request)
    flow_id = required_string(body, "flow_id", detail="flow_id_required")
    try:
        lease = state.oauth_flows.begin_poll(flow_id)
    except FlowBusyError:
        return JSONResponse(status_code=409, content={"error": "flow_poll_in_progress"})
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    consume = False
    try:
        result = await state.workbuddy_intl_oauth.poll(lease.auth_state)
        if result.status == "pending":
            return {"status": "pending"}
        if result.status != "success" or not result.access_token:
            return {"status": "error", "message": result.message or "auth_failed"}
        account_id = await _persist_result(state, lease.record, result)
        consume = True
        return {
            "status": "success",
            "account": await _publish(state, account_id, mutation_action="account.import"),
        }
    finally:
        state.oauth_flows.finish_poll(flow_id, consume=consume)


@router.post("/auth/workbuddy-intl/manual")
async def workbuddy_intl_manual(request: Request) -> dict[str, Any]:
    """Import a pasted international bearer token (with optional refresh token)."""
    await require_admin(request)
    state = admin_state(request)
    body = await json_object(request)
    access_token = required_string(
        body, "token", "access_token", "bearer", detail="token_required"
    )
    refresh_token = _optional_string(body.get("refresh_token"))
    account_id = optional_account_id(body.get("account_id"))
    await _require_intl_account(state, account_id)
    account_id = await persist_workbuddy_intl_account(
        state.account_repo,
        state.credential_vault,
        label=label(body.get("label"), default="workbuddy-intl"),
        source="manual",
        access_token=access_token,
        refresh_token=refresh_token,
        account_id=account_id,
    )
    state.credential_resolver.invalidate(PROVIDER, account_id, "chat")
    return {
        "status": "ok",
        "account": await _publish(state, account_id, mutation_action="account.import"),
    }


@router.delete("/accounts/workbuddy-intl/{account_id}")
async def workbuddy_intl_delete(account_id: str, request: Request) -> dict[str, Any]:
    """Remove an international account and every credential attached to it."""
    await require_admin(request)
    state = admin_state(request)
    if state.account_registry.is_env_account(PROVIDER, account_id):
        raise HTTPException(status_code=400, detail="env_account_read_only")
    if find_account_view(state, PROVIDER, account_id) is None:
        raise HTTPException(status_code=404, detail="account_not_found")
    deleted = await state.account_repo.delete_account(PROVIDER, account_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="account_not_found")
    state.credential_resolver.invalidate(PROVIDER, account_id, "chat")
    await state.account_repo.add_audit_event(
        actor_type="admin",
        actor_id=None,
        action="account.delete",
        resource_type="account",
        resource_id=f"{PROVIDER}:{account_id}",
        result="succeeded",
    )
    await refresh_after_mutation(
        state,
        mutation_action="account.delete",
        resource_type="account",
        resource_id=f"{PROVIDER}:{account_id}",
    )
    return {"status": "deleted", "account_id": account_id}


async def _persist_result(state: Any, record: Any, result: Any) -> str:
    expires_at = _expires_at(result.expires_in)
    account_id = await persist_workbuddy_intl_account(
        state.account_repo,
        state.credential_vault,
        label=record.label,
        source="oauth",
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_at=expires_at,
        account_id=record.account_id,
    )
    state.credential_resolver.invalidate(PROVIDER, account_id, "chat")
    return account_id


async def _publish(state: Any, account_id: str, *, mutation_action: str) -> dict[str, Any]:
    await refresh_after_mutation(
        state,
        mutation_action=mutation_action,
        resource_type="account",
        resource_id=f"{PROVIDER}:{account_id}",
    )
    view = find_account_view(state, PROVIDER, account_id)
    if view is None:
        raise HTTPException(status_code=404, detail="account_not_found")
    return account_view_dict(view)


async def _require_intl_account(state: Any, account_id: str | None) -> None:
    if account_id is None:
        return
    if state.account_registry.is_env_account(PROVIDER, account_id):
        raise HTTPException(status_code=400, detail="cannot_reauthorize_env_account")
    accounts = await state.account_repo.list_accounts(PROVIDER)
    if not any(account["account_id"] == account_id for account in accounts):
        raise HTTPException(status_code=404, detail="account_not_found")


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail="invalid_refresh_token")
    return value.strip()


def _expires_at(expires_in: int | None) -> str | None:
    if not isinstance(expires_in, int) or expires_in <= 0:
        return None
    return (datetime.now(UTC) + timedelta(seconds=expires_in)).replace(microsecond=0).isoformat()
