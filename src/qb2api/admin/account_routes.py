"""Admin account listing, editing, promotion, probing, and deletion."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from qb2api.accounts.promote import promote_env_account
from qb2api.checkin.service import CheckinInProgressError, CheckinTarget

from .dependencies import admin_state, require_admin
from .validation import json_object, label
from .views import account_view_dict, find_account_view

router = APIRouter()
_PATCH_FIELDS = frozenset({"label", "enabled", "purposes"})
_PURPOSES = frozenset({"chat", "checkin"})


@router.get("/accounts")
async def list_accounts(request: Request) -> dict[str, Any]:
    await require_admin(request)
    views = admin_state(request).account_registry.list_views()
    return {"accounts": [account_view_dict(view) for view in views]}


@router.get("/accounts/{provider}/{account_id}")
async def get_account(provider: str, account_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    view = find_account_view(admin_state(request), provider, account_id)
    if view is None:
        raise HTTPException(status_code=404, detail="account_not_found")
    return account_view_dict(view)


@router.delete("/accounts/{provider}/{account_id}")
async def delete_account(provider: str, account_id: str, request: Request) -> dict[str, str]:
    await require_admin(request)
    state = admin_state(request)
    if state.account_registry.is_env_account(provider, account_id):
        raise HTTPException(status_code=400, detail="cannot_delete_env_account")
    if not await state.account_repo.delete_account(provider, account_id):
        raise HTTPException(status_code=404, detail="account_not_found")
    state.credential_resolver.invalidate(provider, account_id)
    await state.refresh_provider_pools()
    return {"status": "ok"}


@router.post("/accounts/{provider}/{account_id}/promote")
async def promote_account(provider: str, account_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    body = await json_object(request, allow_empty=True)
    selected_label = label(body.get("label"), default=account_id)
    try:
        new_id = await promote_env_account(
            state.account_registry,
            state.account_repo,
            state.credential_vault,
            provider=provider,
            account_id=account_id,
            label=selected_label,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    await state.refresh_provider_pools()
    return {
        "status": "ok",
        "account": _published_view(state, provider, new_id),
        "promoted_from": account_id,
    }


@router.post("/accounts/{provider}/{account_id}/verify-checkin")
async def verify_checkin(
    provider: str,
    account_id: str,
    request: Request,
) -> Any:
    await require_admin(request)
    state = admin_state(request)
    try:
        batch = await state.checkin_service.run_batch(
            trigger="verify",
            targets=[CheckinTarget(provider=provider, account_id=account_id)],
            skip_already_done=False,
        )
    except CheckinInProgressError:
        return JSONResponse(status_code=409, content={"error": "checkin_run_in_progress"})
    await state.refresh_provider_pools()
    return {"status": "ok", "run_id": batch.run_id, "results": batch.results}


@router.patch("/accounts/{provider}/{account_id}")
async def patch_account(provider: str, account_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    if state.account_registry.is_env_account(provider, account_id):
        raise HTTPException(status_code=400, detail="cannot_patch_env_account")
    body = await json_object(request)
    if unknown := set(body) - _PATCH_FIELDS:
        raise HTTPException(status_code=400, detail=f"unsupported_fields:{sorted(unknown)}")
    account = await _find_account(state, provider, account_id)
    purposes = await state.account_repo.list_purposes(provider, account_id)
    async with state.account_repo.transaction():
        await _update_account(state, account, body)
        await _update_purposes(state, provider, account_id, purposes, body)
    await state.refresh_provider_pools()
    return _published_view(state, provider, account_id)


async def _find_account(state: Any, provider: str, account_id: str) -> dict[str, Any]:
    accounts = await state.account_repo.list_accounts(provider)
    account = next((row for row in accounts if row["account_id"] == account_id), None)
    if account is None:
        raise HTTPException(status_code=404, detail="account_not_found")
    return account


async def _update_account(state: Any, account: dict[str, Any], body: dict[str, Any]) -> None:
    await state.account_repo.upsert_account(
        provider=account["provider"],
        account_id=account["account_id"],
        label=label(body.get("label"), default=account["label"]),
        source=account.get("source") or "manual",
        enabled=bool(body.get("enabled", account["enabled"])),
        masked_identity=account.get("masked_identity"),
        identity_hash=account.get("identity_hash"),
    )


async def _update_purposes(
    state: Any,
    provider: str,
    account_id: str,
    purposes: list[dict[str, Any]],
    body: dict[str, Any],
) -> None:
    patches = body.get("purposes", {})
    if not isinstance(patches, dict) or set(patches) - _PURPOSES:
        raise HTTPException(status_code=400, detail="invalid_purposes")
    for current in purposes:
        patch = patches.get(current["purpose"])
        if patch is None:
            continue
        if not isinstance(patch, dict) or set(patch) - {"enabled"}:
            raise HTTPException(status_code=400, detail="invalid_purpose_patch")
        await state.account_repo.upsert_purpose(
            provider=provider,
            account_id=account_id,
            purpose=current["purpose"],
            enabled=bool(patch.get("enabled", current["enabled"])),
            status=current["status"],
            verification_status=current["verification_status"],
            capabilities=current.get("capabilities"),
            verified_at=current.get("verified_at"),
            expires_at=current.get("expires_at"),
            last_success_at=current.get("last_success_at"),
            failure_count=current.get("failure_count", 0),
            last_error=current.get("last_error"),
        )


def _published_view(state: Any, provider: str, account_id: str) -> dict[str, Any]:
    view = find_account_view(state, provider, account_id)
    if view is None:
        raise HTTPException(status_code=404, detail="account_not_found")
    return account_view_dict(view)
