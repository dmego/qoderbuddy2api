"""Secret-safe repository helpers for credential administration."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from .dependencies import admin_state


def repository(request: Request):
    selected = getattr(admin_state(request), "account_repo", None)
    if selected is None:
        raise HTTPException(status_code=503, detail="repository_unavailable")
    return selected


async def current_credential(
    repository: Any,
    *,
    provider: str,
    account_id: str,
    purpose: str,
) -> dict[str, Any]:
    current = await repository.get_credential(provider, account_id, purpose)
    if current is None:
        raise HTTPException(status_code=404, detail="credential_not_found")
    return current


async def purpose_or_404(
    repository: Any,
    *,
    provider: str,
    account_id: str,
    purpose: str,
) -> dict[str, Any]:
    purposes = await repository.list_purposes(provider, account_id)
    current = next((item for item in purposes if item["purpose"] == purpose), None)
    if current is None:
        raise HTTPException(status_code=409, detail="credential_purpose_not_configured")
    return current


async def mark_unverified_purpose(
    repository: Any,
    *,
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
