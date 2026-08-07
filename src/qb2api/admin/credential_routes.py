"""Credential rotation and revocation endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from qb2api.accounts.repository import CredentialVersionConflict

from .credential_payload import expected_version, expires_at, rotation_payload
from .credential_repository import (
    current_credential,
    mark_unverified_purpose,
    purpose_or_404,
    repository,
)
from .dependencies import admin_state, require_admin
from .mutation_audit import add_audit, refresh_after_mutation
from .validation import json_object

router = APIRouter()
_ROTATION_FIELDS = frozenset(
    {
        "token", "access_token", "pat", "cookie", "mode", "refresh_token",
        "expires_at", "credential_version",
    }
)


@router.get("/credentials")
async def credential_metadata(request: Request, provider: str | None = None) -> dict[str, Any]:
    await require_admin(request)
    return {"credentials": await repository(request).list_credential_metadata(provider)}


@router.post("/credentials/{provider}/{account_id}/{purpose}/rotate")
async def rotate_credential_action(
    request: Request,
    *,
    provider: str,
    account_id: str,
    purpose: str,
) -> dict[str, Any]:
    return await _rotate_credential(
        request,
        provider=provider,
        account_id=account_id,
        purpose=purpose,
    )


@router.patch("/credentials/{provider}/{account_id}/{purpose}")
async def rotate_credential(
    request: Request,
    *,
    provider: str,
    account_id: str,
    purpose: str,
) -> dict[str, Any]:
    """Compatibility alias retained for one migration cycle."""
    return await _rotate_credential(
        request,
        provider=provider,
        account_id=account_id,
        purpose=purpose,
    )


async def _rotate_credential(
    request: Request,
    *,
    provider: str,
    account_id: str,
    purpose: str,
) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    _validate_rotatable(state, provider=provider, account_id=account_id, purpose=purpose)
    body = await json_object(request)
    _validate_rotation_fields(body)
    account_repository = repository(request)
    current = await current_credential(
        account_repository,
        provider=provider,
        account_id=account_id,
        purpose=purpose,
    )
    purpose_state = await purpose_or_404(
        account_repository,
        provider=provider,
        account_id=account_id,
        purpose=purpose,
    )
    expires = expires_at(body, current.get("expires_at"))
    mode, payload, fingerprint = rotation_payload(
        provider=provider,
        purpose=purpose,
        current=current,
        body=body,
    )
    version = await _persist_rotation(
        account_repository,
        state=state,
        provider=provider,
        account_id=account_id,
        purpose=purpose,
        purpose_state=purpose_state,
        mode=mode,
        payload=payload,
        fingerprint=fingerprint,
        expires_at=expires,
        credential_version=expected_version(body),
    )
    await _refresh_rotated_credential(state, provider=provider, account_id=account_id, purpose=purpose)
    return {"status": "succeeded", "credential_version": version, "verification_status": "unverified"}


def _validate_rotatable(state: Any, *, provider: str, account_id: str, purpose: str) -> None:
    if purpose not in {"chat", "checkin"} or state.account_registry.is_env_account(provider, account_id):
        raise HTTPException(status_code=400, detail="credential_not_rotatable")


def _validate_rotation_fields(body: dict[str, Any]) -> None:
    if unknown := set(body) - _ROTATION_FIELDS:
        raise HTTPException(status_code=400, detail=f"unsupported_fields:{sorted(unknown)}")


async def _persist_rotation(
    repository: Any,
    *,
    state: Any,
    provider: str,
    account_id: str,
    purpose: str,
    purpose_state: dict[str, Any],
    mode: str,
    payload: dict[str, str],
    fingerprint: str,
    expires_at: str | None,
    credential_version: int | None,
) -> int:
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
                expected_version=credential_version,
            )
            await mark_unverified_purpose(
                repository,
                provider=provider,
                account_id=account_id,
                purpose=purpose,
                current=purpose_state,
                expires_at=expires_at,
                enabled=True,
            )
            await add_audit(
                repository,
                action="credential.rotate",
                resource_type="credential",
                resource_id=f"{provider}/{account_id}/{purpose}",
            )
    except CredentialVersionConflict as error:
        raise HTTPException(status_code=409, detail="credential_version_conflict") from error
    return version


async def _refresh_rotated_credential(
    state: Any,
    *,
    provider: str,
    account_id: str,
    purpose: str,
) -> None:
    state.credential_resolver.invalidate(provider, account_id, purpose)
    await refresh_after_mutation(
        state,
        mutation_action="credential.rotate",
        resource_type="credential",
        resource_id=f"{provider}/{account_id}/{purpose}",
    )


@router.post("/credentials/{provider}/{account_id}/{purpose}/revoke")
async def revoke_credential_action(
    request: Request,
    *,
    provider: str,
    account_id: str,
    purpose: str,
) -> dict[str, str]:
    return await _revoke_credential(
        request,
        provider=provider,
        account_id=account_id,
        purpose=purpose,
    )


@router.delete("/credentials/{provider}/{account_id}/{purpose}")
async def revoke_credential(
    request: Request,
    *,
    provider: str,
    account_id: str,
    purpose: str,
) -> dict[str, str]:
    """Compatibility alias retained for one migration cycle."""
    return await _revoke_credential(
        request,
        provider=provider,
        account_id=account_id,
        purpose=purpose,
    )


async def _revoke_credential(
    request: Request,
    *,
    provider: str,
    account_id: str,
    purpose: str,
) -> dict[str, str]:
    await require_admin(request)
    state = admin_state(request)
    _validate_revokeable(state, provider=provider, account_id=account_id, purpose=purpose)
    account_repository = repository(request)
    purpose_state = await purpose_or_404(
        account_repository,
        provider=provider,
        account_id=account_id,
        purpose=purpose,
    )
    async with account_repository.transaction():
        if not await account_repository.delete_credential(provider, account_id, purpose):
            raise HTTPException(status_code=404, detail="credential_not_found")
        await mark_unverified_purpose(
            account_repository,
            provider=provider,
            account_id=account_id,
            purpose=purpose,
            current=purpose_state,
            expires_at=purpose_state.get("expires_at"),
            enabled=False,
        )
        await add_audit(
            account_repository,
            action="credential.revoke",
            resource_type="credential",
            resource_id=f"{provider}/{account_id}/{purpose}",
        )
    state.credential_resolver.invalidate(provider, account_id, purpose)
    await refresh_after_mutation(
        state,
        mutation_action="credential.revoke",
        resource_type="credential",
        resource_id=f"{provider}/{account_id}/{purpose}",
    )
    return {"status": "succeeded"}


def _validate_revokeable(state: Any, *, provider: str, account_id: str, purpose: str) -> None:
    if purpose not in {"chat", "checkin"} or state.account_registry.is_env_account(provider, account_id):
        raise HTTPException(status_code=400, detail="credential_not_revokeable")
