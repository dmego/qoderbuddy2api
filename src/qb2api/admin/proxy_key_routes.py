"""Admin-only lifecycle for one-time-revealed Proxy API Keys."""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .crypto import hash_token
from .dependencies import admin_state, require_admin
from .validation import json_object, label

router = APIRouter()
logger = logging.getLogger("qb2api.admin.proxy_keys")
_ALLOWED_FIELDS = frozenset({"name", "expires_at", "scopes"})


@router.get("/proxy-keys")
async def list_proxy_keys(request: Request) -> dict[str, Any]:
    await require_admin(request)
    return {"keys": await _repository(request).list_proxy_api_keys()}


@router.post("/proxy-keys", status_code=201)
async def create_proxy_key(request: Request) -> dict[str, Any]:
    await require_admin(request)
    body = await json_object(request, allow_empty=True)
    _validate_fields(body)
    key_id, raw = _new_key()
    expires_at = _expiry(body.get("expires_at"))
    key_name = label(body.get("name"), default="Proxy key")
    await _repository(request).create_proxy_api_key(
        key_id=key_id,
        name=key_name,
        key_hash=hash_token(raw),
        expires_at=expires_at,
    )
    publication = await _after_key_change(request, "proxy_key.create", key_id)
    return {
        "key_id": key_id,
        "key": raw,
        "name": key_name,
        "expires_at": expires_at,
        **publication,
    }


@router.post("/proxy-keys/{key_id}/revoke")
async def revoke_proxy_key(key_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    if not await _repository(request).revoke_proxy_api_key(key_id):
        raise HTTPException(status_code=404, detail="proxy_key_not_found")
    publication = await _after_key_change(request, "proxy_key.revoke", key_id)
    status = "succeeded" if publication["runtime_apply"]["status"] == "succeeded" else "runtime_pending"
    return {"status": status, "key_id": key_id, **publication}


@router.post("/proxy-keys/{key_id}/rotate", status_code=201)
async def rotate_proxy_key(key_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    repository = _repository(request)
    current = next(
        (item for item in await repository.list_proxy_api_keys() if item["key_id"] == key_id),
        None,
    )
    if current is None or not current["enabled"]:
        raise HTTPException(status_code=404, detail="proxy_key_not_found")
    replacement_id, raw = _new_key()
    async with repository.transaction():
        if not await repository.revoke_proxy_api_key(key_id):
            raise HTTPException(status_code=409, detail="proxy_key_changed")
        await repository.create_proxy_api_key(
            key_id=replacement_id,
            name=current["name"],
            key_hash=hash_token(raw),
            expires_at=current.get("expires_at"),
        )
    publication = await _after_key_change(request, "proxy_key.rotate", replacement_id)
    return {
        "key_id": replacement_id,
        "replaced_key_id": key_id,
        "key": raw,
        "name": current["name"],
        "expires_at": current.get("expires_at"),
        **publication,
    }


def _repository(request: Request):
    repository = getattr(admin_state(request), "account_repo", None)
    if repository is None:
        raise HTTPException(status_code=503, detail="repository_unavailable")
    return repository


def _new_key() -> tuple[str, str]:
    return f"pk_{uuid.uuid4().hex}", f"qb2api_{secrets.token_urlsafe(32)}"


def _validate_fields(body: dict[str, Any]) -> None:
    if unknown := set(body) - _ALLOWED_FIELDS:
        raise HTTPException(status_code=400, detail=f"unsupported_fields:{sorted(unknown)}")
    scopes = body.get("scopes")
    if scopes is not None and scopes != ["proxy"]:
        raise HTTPException(status_code=400, detail="proxy_key_scope_invalid")


def _expiry(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise HTTPException(status_code=400, detail="invalid_expires_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail="invalid_expires_at") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if parsed <= datetime.now(UTC):
        raise HTTPException(status_code=400, detail="expires_at_must_be_future")
    return parsed.astimezone(UTC).isoformat()


async def _after_key_change(
    request: Request,
    action: str,
    key_id: str,
) -> dict[str, dict[str, str]]:
    state = admin_state(request)
    runtime_apply = await _publish_runtime(state, action, key_id)
    audit_status = await _record_audit(
        request,
        action,
        key_id,
        runtime_apply["status"],
    )
    return {"runtime_apply": runtime_apply, "audit": audit_status}


async def _publish_runtime(
    state: Any,
    action: str,
    key_id: str,
) -> dict[str, str]:
    try:
        await state.refresh_provider_pools()
    except Exception:
        logger.exception("%s runtime apply failed for key_id=%s", action, key_id)
        return {"status": "failed", "error_code": "runtime_reload_failed"}
    return {"status": "succeeded"}


async def _record_audit(
    request: Request,
    action: str,
    key_id: str,
    runtime_status: str,
) -> dict[str, str]:
    result = "succeeded" if runtime_status == "succeeded" else "failed"
    try:
        await _repository(request).add_audit_event(
            actor_type="admin",
            actor_id=None,
            action=action,
            resource_type="proxy_key",
            resource_id=key_id,
            result=result,
            metadata={"runtime_apply": runtime_status},
        )
    except Exception:
        logger.exception("%s audit write failed for key_id=%s", action, key_id)
        return {"status": "failed", "error_code": "audit_write_failed"}
    return {"status": "succeeded"}
