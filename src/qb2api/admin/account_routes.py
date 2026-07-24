"""Admin account listing, editing, promotion, probing, and deletion."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from qb2api.accounts.promote import promote_env_account
from qb2api.checkin.service import CheckinInProgressError, CheckinTarget

from .catalog_routes import ProbeError, probe_model_for_account
from .dependencies import admin_state, require_admin
from .validation import (
    bounded_int,
    choice_filter,
    cursor_value,
    json_object,
    label,
    page_slice,
    provider_filter,
    text_filter,
)
from .views import account_view_dict, find_account_view

router = APIRouter()
_PATCH_FIELDS = frozenset({"label", "enabled", "purposes"})
_PURPOSES = frozenset({"chat", "checkin"})


@router.get("/accounts")
async def list_accounts(
    request: Request,
    provider: str | None = None,
    source: str | None = None,
    status: str | None = None,
    purpose: str | None = None,
    query: str | None = None,
    cursor: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    await require_admin(request)
    views = admin_state(request).account_registry.list_views()
    selected = _filter_accounts(
        [account_view_dict(view) for view in views],
        provider=provider_filter(provider),
        source=choice_filter(
            source,
            frozenset({"env", "oauth", "manual", "import"}),
            detail="invalid_source",
        ),
        status=choice_filter(
            status,
            frozenset({"active", "disabled", "action_required", "pending"}),
            detail="invalid_status",
        ),
        purpose=choice_filter(purpose, _PURPOSES, detail="invalid_purpose"),
        query=text_filter(query, detail="invalid_query"),
    )
    selected_limit = bounded_int(limit, default=100, maximum=100)
    page, next_cursor = page_slice(
        selected,
        cursor_value(cursor, allow_zero=True),
        selected_limit,
    )
    return {"accounts": page, "limit": selected_limit, "next_cursor": next_cursor}


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
    await _audit(request, "account.delete", provider, account_id)
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
        raise HTTPException(status_code=404, detail="account_not_found") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail="account_promotion_rejected") from error
    await state.refresh_provider_pools()
    await _audit(request, "account.promote", provider, new_id)
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
    await _audit(request, "account.verify_checkin", provider, account_id)
    return {"status": "ok", "run_id": batch.run_id, "results": batch.results}


@router.post("/accounts/{provider}/{account_id}/refresh")
async def refresh_account(provider: str, account_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    await _empty_mutation_body(request, "refresh_body_not_allowed")
    state = admin_state(request)
    if find_account_view(state, provider, account_id) is None:
        raise HTTPException(status_code=404, detail="account_not_found")
    state.credential_resolver.invalidate(provider, account_id)
    await state.refresh_provider_pools()
    await _audit(request, "account.refresh", provider, account_id)
    return {
        "status": "succeeded",
        "account": _published_view(state, provider, account_id),
    }


@router.post("/accounts/{provider}/{account_id}/probe")
async def probe_account(provider: str, account_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    await _empty_mutation_body(request, "probe_body_not_allowed")
    state = admin_state(request)
    if find_account_view(state, provider, account_id) is None:
        raise HTTPException(status_code=404, detail="account_not_found")
    try:
        result = await probe_model_for_account(state, provider, account_id)
    except ProbeError as error:
        await _audit(request, "account.probe", provider, account_id, result="failed")
        raise HTTPException(status_code=error.status_code, detail=error.code) from error
    await _audit(request, "account.probe", provider, account_id)
    return result


@router.patch("/accounts/{provider}/{account_id}")
async def patch_account(provider: str, account_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    if state.account_registry.is_env_account(provider, account_id):
        raise HTTPException(status_code=400, detail="cannot_patch_env_account")
    body = await json_object(request)
    if set(body) - _PATCH_FIELDS:
        raise HTTPException(status_code=400, detail="unsupported_fields")
    account = await _find_account(state, provider, account_id)
    purposes = await state.account_repo.list_purposes(provider, account_id)
    async with state.account_repo.transaction():
        await _update_account(state, account, body)
        await _update_purposes(state, provider, account_id, purposes, body)
    await state.refresh_provider_pools()
    await _audit(request, "account.update", provider, account_id)
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


def _filter_accounts(
    accounts: list[dict[str, Any]],
    *,
    provider: str | None,
    source: str | None,
    status: str | None,
    purpose: str | None,
    query: str | None,
) -> list[dict[str, Any]]:
    needle = query.lower() if query else None
    return [
        account
        for account in accounts
        if (provider is None or account["provider"] == provider)
        and (source is None or account["source"] == source)
        and (status is None or account["summary_status"] == status)
        and (purpose is None or purpose in account["purposes"])
        and (
            needle is None
            or needle in f"{account['label']} {account['account_id']}".lower()
        )
    ]


async def _empty_mutation_body(request: Request, detail: str) -> None:
    body = await json_object(request, allow_empty=True)
    if body:
        raise HTTPException(status_code=400, detail=detail)


async def _audit(
    request: Request,
    action: str,
    provider: str,
    account_id: str,
    *,
    result: str = "succeeded",
) -> None:
    repository = getattr(admin_state(request), "account_repo", None)
    if repository is None:
        return
    await repository.add_audit_event(
        actor_type="admin",
        actor_id=None,
        action=action,
        resource_type="account",
        resource_id=f"{provider}:{account_id}",
        result=result,
    )
