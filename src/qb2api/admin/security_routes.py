"""Credential metadata and backup administration without secret disclosure."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from qb2api.accounts.repository import CredentialVersionConflict

from .dependencies import admin_state, require_admin
from .mutation_audit import add_audit, audit_operation, refresh_after_mutation
from .validation import bounded_int, choice_filter, cursor_value, json_object

router = APIRouter()
_ROTATION_FIELDS = frozenset(
    {
        "token", "access_token", "pat", "cookie", "mode", "refresh_token",
        "expires_at", "credential_version",
    }
)
_CREDENTIAL_MODES = frozenset({"bearer", "cookie", "bearer_cookie", "pat", "access_refresh", "oauth"})


@router.get("/credentials")
async def credential_metadata(request: Request, provider: str | None = None) -> dict[str, Any]:
    await require_admin(request)
    repository = _repository(request)
    return {"credentials": await repository.list_credential_metadata(provider)}


@router.post("/credentials/{provider}/{account_id}/{purpose}/rotate")
async def rotate_credential_action(
    provider: str,
    account_id: str,
    purpose: str,
    request: Request,
) -> dict[str, Any]:
    return await _rotate_credential(provider, account_id, purpose, request)


@router.patch("/credentials/{provider}/{account_id}/{purpose}")
async def rotate_credential(
    provider: str,
    account_id: str,
    purpose: str,
    request: Request,
) -> dict[str, Any]:
    """Compatibility alias retained for one migration cycle."""
    return await _rotate_credential(provider, account_id, purpose, request)


async def _rotate_credential(
    provider: str,
    account_id: str,
    purpose: str,
    request: Request,
) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    if purpose not in {"chat", "checkin"} or state.account_registry.is_env_account(provider, account_id):
        raise HTTPException(status_code=400, detail="credential_not_rotatable")
    body = await json_object(request)
    if unknown := set(body) - _ROTATION_FIELDS:
        raise HTTPException(status_code=400, detail=f"unsupported_fields:{sorted(unknown)}")
    repository = _repository(request)
    current = await repository.get_credential(provider, account_id, purpose)
    if current is None:
        raise HTTPException(status_code=404, detail="credential_not_found")
    purpose_state = await _purpose_or_404(repository, provider, account_id, purpose)
    expires_at = _expires_at(body, current.get("expires_at"))
    mode, payload, fingerprint = _rotation_payload(provider, purpose, current, body)
    try:
        async with repository.transaction():
            version = await repository.upsert_credential(
                provider=provider,
                account_id=account_id,
                purpose=purpose,
                mode=mode,
                encrypted_payload=state.credential_vault.encrypt(payload),
                has_refresh_token=bool(payload.get("refresh_token")),
                expires_at=expires_at,
                fingerprint_hmac=state.credential_vault.fingerprint(fingerprint),
                expected_version=_expected_version(body),
            )
            await _mark_unverified_purpose(
                repository, provider, account_id, purpose, purpose_state, expires_at, True
            )
            await add_audit(
                repository, action="credential.rotate", resource_type="credential",
                resource_id=f"{provider}/{account_id}/{purpose}",
            )
    except CredentialVersionConflict as error:
        raise HTTPException(status_code=409, detail="credential_version_conflict") from error
    state.credential_resolver.invalidate(provider, account_id, purpose)
    await refresh_after_mutation(
        state, mutation_action="credential.rotate", resource_type="credential",
        resource_id=f"{provider}/{account_id}/{purpose}",
    )
    return {"status": "succeeded", "credential_version": version, "verification_status": "unverified"}


@router.post("/credentials/{provider}/{account_id}/{purpose}/revoke")
async def revoke_credential_action(
    provider: str,
    account_id: str,
    purpose: str,
    request: Request,
) -> dict[str, str]:
    return await _revoke_credential(provider, account_id, purpose, request)


@router.delete("/credentials/{provider}/{account_id}/{purpose}")
async def revoke_credential(
    provider: str,
    account_id: str,
    purpose: str,
    request: Request,
) -> dict[str, str]:
    """Compatibility alias retained for one migration cycle."""
    return await _revoke_credential(provider, account_id, purpose, request)


async def _revoke_credential(
    provider: str,
    account_id: str,
    purpose: str,
    request: Request,
) -> dict[str, str]:
    await require_admin(request)
    state = admin_state(request)
    if purpose not in {"chat", "checkin"} or state.account_registry.is_env_account(provider, account_id):
        raise HTTPException(status_code=400, detail="credential_not_revokeable")
    repository = _repository(request)
    purpose_state = await _purpose_or_404(repository, provider, account_id, purpose)
    async with repository.transaction():
        if not await repository.delete_credential(provider, account_id, purpose):
            raise HTTPException(status_code=404, detail="credential_not_found")
        await _mark_unverified_purpose(
            repository, provider, account_id, purpose, purpose_state, purpose_state.get("expires_at"), False
        )
        await add_audit(
            repository, action="credential.revoke", resource_type="credential",
            resource_id=f"{provider}/{account_id}/{purpose}",
        )
    state.credential_resolver.invalidate(provider, account_id, purpose)
    await refresh_after_mutation(
        state, mutation_action="credential.revoke", resource_type="credential",
        resource_id=f"{provider}/{account_id}/{purpose}",
    )
    return {"status": "succeeded"}


@router.get("/backup")
async def list_backups(
    request: Request,
    *,
    limit: str | None = None,
    cursor: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    await require_admin(request)
    selected_limit = bounded_int(limit, default=50, maximum=100)
    selected_status = choice_filter(
        status, {"running", "succeeded", "failed", "cancelled"}, detail="invalid_status"
    )
    rows = await _repository(request).list_backup_runs(limit=500)
    if selected_status is not None:
        rows = [row for row in rows if row.get("status") == selected_status]
    offset = cursor_value(cursor, allow_zero=True) or 0
    page = rows[offset:offset + selected_limit]
    next_cursor = str(offset + selected_limit) if len(rows) > offset + selected_limit else None
    return {
        "backups": page,
        "limit": selected_limit,
        "next_cursor": next_cursor,
        "total": len(rows),
    }


@router.post("/backup")
async def create_backup(request: Request) -> dict[str, Any]:
    await require_admin(request)
    try:
        async with audit_operation(
            _repository(request), action="backup.create", resource_type="backup",
            resource_id="create", failure_code="backup_creation_failed",
        ):
            result = await _service(request).create()
    except Exception as error:
        raise HTTPException(status_code=422, detail="backup_creation_failed") from error
    return result


@router.get("/backup/{backup_id}")
async def get_backup(backup_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    service = _service(request)
    try:
        return await service.get(backup_id)
    except Exception as error:
        raise HTTPException(status_code=404, detail="backup_not_found") from error


@router.post("/backup/{backup_id}/restore")
async def validate_restore(backup_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    body = await json_object(request, allow_empty=True)
    if body.get("dry_run", True) is not True:
        raise HTTPException(status_code=409, detail="offline_restore_required")
    try:
        async with audit_operation(
            _repository(request), action="backup.restore.validate",
            resource_type="backup", resource_id=backup_id,
            failure_code="backup_validation_failed",
        ):
            result = await _service(request).validate_restore(backup_id)
    except Exception as error:
        raise HTTPException(status_code=422, detail="backup_validation_failed") from error
    return result


def _repository(request: Request):
    repository = getattr(admin_state(request), "account_repo", None)
    if repository is None:
        raise HTTPException(status_code=503, detail="repository_unavailable")
    return repository


def _service(request: Request):
    service = getattr(admin_state(request), "backup_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="backup_service_unavailable")
    return service


def _rotation_payload(
    provider: str,
    purpose: str,
    current: dict[str, Any],
    body: dict[str, Any],
) -> tuple[str, dict[str, str], str]:
    mode = body.get("mode", current.get("mode") or "bearer")
    if not isinstance(mode, str) or mode not in _CREDENTIAL_MODES:
        raise HTTPException(status_code=400, detail="invalid_credential_mode")
    access = _credential_value(body, "token", "access_token", "pat")
    cookie = _credential_value(body, "cookie")
    if mode in {"bearer", "bearer_cookie", "access_refresh", "pat", "oauth"} and not access:
        raise HTTPException(status_code=400, detail="token_required")
    if mode in {"cookie", "bearer_cookie"} and not cookie:
        raise HTTPException(status_code=400, detail="cookie_required")
    if provider == "qoder" and purpose == "chat" and mode != "pat":
        raise HTTPException(status_code=400, detail="invalid_credential_mode")
    payload: dict[str, str] = {"pat" if mode == "pat" else "access_token": access} if access else {}
    if cookie:
        payload["cookie"] = cookie
    refresh = _credential_value(body, "refresh_token")
    if refresh:
        payload["refresh_token"] = refresh
    return mode, payload, access or cookie or ""


def _credential_value(body: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(status_code=400, detail=f"invalid_{key}")
        return value.strip()
    return None


def _expires_at(body: dict[str, Any], fallback: str | None) -> str | None:
    value = body.get("expires_at", fallback)
    if value is not None and not isinstance(value, str):
        raise HTTPException(status_code=400, detail="invalid_expires_at")
    return value


def _expected_version(body: dict[str, Any]) -> int | None:
    value = body.get("credential_version")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HTTPException(status_code=400, detail="invalid_credential_version")
    return value


async def _purpose_or_404(repository: Any, provider: str, account_id: str, purpose: str) -> dict[str, Any]:
    purposes = await repository.list_purposes(provider, account_id)
    current = next((item for item in purposes if item["purpose"] == purpose), None)
    if current is None:
        raise HTTPException(status_code=409, detail="credential_purpose_not_configured")
    return current


async def _mark_unverified_purpose(
    repository: Any,
    provider: str,
    account_id: str,
    purpose: str,
    current: dict[str, Any],
    expires_at: str | None,
    enabled: bool,
) -> None:
    await repository.upsert_purpose(
        provider=provider,
        account_id=account_id,
        purpose=purpose,
        enabled=enabled,
        status="needs_reauth",
        verification_status="unverified",
        capabilities=current.get("capabilities"),
        expires_at=expires_at,
        last_success_at=current.get("last_success_at"),
        failure_count=current.get("failure_count", 0),
        last_error="credential_rotated_needs_verification" if enabled else "credential_revoked",
    )
