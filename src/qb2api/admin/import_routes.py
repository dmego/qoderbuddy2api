"""CodeBuddy OAuth/manual and Qoder credential import routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from qb2api.accounts.imports import (
    persist_codebuddy_account,
    persist_qoder_chat,
    persist_qoder_checkin,
)
from qb2api.auth.codebuddy_oauth import CodeBuddyOAuthError
from qb2api.auth.flows import FlowBusyError
from qb2api.checkin.models import CheckInOutcome

from .dependencies import admin_state, require_admin
from .mutation_audit import add_audit, refresh_after_mutation
from .validation import json_object, label, optional_account_id, required_string
from .views import account_view_dict, find_account_view

router = APIRouter()


@router.post("/auth/codebuddy/start")
async def codebuddy_oauth_start(request: Request) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    if not state.settings.codebuddy_oauth_enabled:
        raise HTTPException(status_code=400, detail="oauth_disabled")
    body = await json_object(request, allow_empty=True)
    selected_label = label(body.get("label"), default="codebuddy")
    try:
        started = await state.codebuddy_oauth.start()
    except CodeBuddyOAuthError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    flow = state.oauth_flows.create(
        label=selected_label,
        auth_state=started.auth_state,
        auth_url=started.auth_url,
    )
    return {
        "flow_id": flow.flow_id,
        "auth_url": flow.auth_url,
        "expires_at": datetime.fromtimestamp(flow.expires_at, tz=UTC)
        .replace(microsecond=0)
        .isoformat(),
        "label": flow.label,
    }


@router.post("/auth/codebuddy/poll")
async def codebuddy_oauth_poll(request: Request) -> Any:
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
        result = await state.codebuddy_oauth.poll(lease.auth_state)
        if result.status == "pending":
            return {"status": "pending"}
        if result.status != "success" or not result.access_token:
            return {"status": "error", "message": result.message or "auth_failed"}
        account_id = await _persist_oauth_result(state, lease.record.label, result)
        consume = True
        return {
            "status": "success",
            "account": await _publish(
                state, "codebuddy", account_id, mutation_action="account.import"
            ),
        }
    finally:
        state.oauth_flows.finish_poll(flow_id, consume=consume)


@router.post("/auth/codebuddy/manual")
async def codebuddy_manual(request: Request) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    body = await json_object(request)
    access_token = required_string(
        body, "token", "access_token", "bearer", detail="token_required"
    )
    account_id = await persist_codebuddy_account(
        state.account_repo,
        state.credential_vault,
        label=label(body.get("label"), default="manual"),
        source="manual",
        access_token=access_token,
    )
    return {
        "status": "ok",
        "account": await _publish(
            state, "codebuddy", account_id, mutation_action="account.import"
        ),
    }


@router.post("/auth/qoder/chat")
async def qoder_chat_import(request: Request) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    body = await json_object(request)
    pat = required_string(body, "pat", "token", detail="pat_required")
    try:
        account_id = await persist_qoder_chat(
            state.account_repo,
            state.credential_vault,
            label=label(body.get("label"), default="qoder"),
            pat=pat,
            account_id=optional_account_id(body.get("account_id")),
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail="account_not_found") from error
    state.credential_resolver.invalidate("qoder", account_id, "chat")
    return {
        "status": "ok",
        "account": await _publish(
            state, "qoder", account_id, mutation_action="account.import"
        ),
    }


@router.post("/auth/qoder/checkin")
async def qoder_checkin_import(request: Request) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    body = await json_object(request)
    account_id = required_string(body, "account_id", detail="account_id_required")
    access_token = required_string(
        body, "access_token", "device_token", "token", detail="access_token_required"
    )
    refresh_token = required_string(body, "refresh_token", detail="refresh_token_required")
    await _require_qoder_account(state, account_id)
    probe = await state.checkin_service.qoder_client.status(
        access_token=access_token,
        account_id=account_id,
    )
    if probe.outcome not in {CheckInOutcome.ALREADY_CHECKED_IN, CheckInOutcome.SKIPPED}:
        raise HTTPException(status_code=400, detail="checkin_credential_rejected")
    await persist_qoder_checkin(
        state.account_repo,
        state.credential_vault,
        account_id=account_id,
        access_token=access_token,
        refresh_token=refresh_token,
        verified_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
    )
    state.credential_resolver.invalidate("qoder", account_id, "checkin")
    return {
        "status": "ok",
        "account": await _publish(
            state, "qoder", account_id, mutation_action="credential.import"
        ),
    }


async def _persist_oauth_result(state: Any, selected_label: str, result: Any) -> str:
    expires_at = None
    if result.expires_in:
        expires_at = (
            datetime.now(UTC) + timedelta(seconds=int(result.expires_in))
        ).replace(microsecond=0).isoformat()
    return await persist_codebuddy_account(
        state.account_repo,
        state.credential_vault,
        label=selected_label,
        source="oauth",
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_at=expires_at,
    )


async def _require_qoder_account(state: Any, account_id: str) -> None:
    accounts = await state.account_repo.list_accounts("qoder")
    if not any(account["account_id"] == account_id for account in accounts):
        raise HTTPException(status_code=404, detail="account_not_found")


async def _publish(
    state: Any,
    provider: str,
    account_id: str,
    *,
    mutation_action: str,
) -> dict[str, Any]:
    resource_id = f"{provider}:{account_id}"
    await refresh_after_mutation(
        state,
        mutation_action=mutation_action,
        resource_type="account",
        resource_id=resource_id,
    )
    view = find_account_view(state, provider, account_id)
    if view is None:
        await add_audit(
            state.account_repo,
            action="provider_pool.refresh",
            resource_type="account",
            resource_id=resource_id,
            result="failed",
            metadata={
                "mutation_action": mutation_action,
                "error_code": "account_publish_failed",
            },
        )
        raise HTTPException(status_code=500, detail="account_publish_failed")
    return account_view_dict(view)
