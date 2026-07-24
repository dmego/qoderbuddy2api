"""Model catalog administration endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from qb2api.models import load_models_from_config

from .dependencies import admin_state, require_admin
from .validation import json_object

router = APIRouter(prefix="/models")


@router.get("")
async def list_models(request: Request, provider: str | None = None) -> dict[str, Any]:
    await require_admin(request)
    repository = _repository(request)
    return {"models": await repository.list_models(provider)}


@router.patch("/{provider}/{model_id}")
async def patch_model(provider: str, model_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    body = await json_object(request)
    if set(body) != {"enabled"} or not isinstance(body["enabled"], bool):
        raise HTTPException(status_code=400, detail="enabled_boolean_required")
    repository = _repository(request)
    if not await repository.set_model_enabled(provider, model_id, body["enabled"]):
        raise HTTPException(status_code=404, detail="model_not_found")
    await _audit(request, "model.update", provider, model_id)
    models = await repository.list_models(provider)
    return next(model for model in models if model["model_id"] == model_id)


@router.post("/refresh")
async def refresh_models(request: Request) -> dict[str, Any]:
    await require_admin(request)
    repository = _repository(request)
    definitions = load_models_from_config(admin_state(request).settings.model_config_path)
    count = 0
    for provider, models in definitions.items():
        for model in models:
            await repository.upsert_model(
                provider=provider,
                model_id=model.id,
                display_name=model.id,
                capabilities=["chat", "streaming"],
                source="definition",
            )
            count += 1
    await _audit(request, "model.refresh", "catalog", "catalog")
    return {"status": "succeeded", "refreshed": count}


def _repository(request: Request):
    repository = getattr(admin_state(request), "account_repo", None)
    if repository is None:
        raise HTTPException(status_code=503, detail="repository_unavailable")
    return repository


async def _audit(request: Request, action: str, resource_type: str, resource_id: str) -> None:
    repository = getattr(admin_state(request), "account_repo", None)
    if repository is not None:
        await repository.add_audit_event(
            actor_type="admin", actor_id=None, action=action,
            resource_type=resource_type, resource_id=resource_id, result="succeeded",
        )
