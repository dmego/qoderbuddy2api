"""Model catalog administration endpoints."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from qb2api.models import load_models_from_config
from qb2api.openai import ChatCompletionRequest, ChatMessage
from qb2api.provider_factory import ProviderFactory

from .dependencies import admin_state, require_admin
from .validation import (
    bool_filter,
    bounded_int,
    cursor_value,
    json_object,
    page_slice,
    provider_filter,
    text_filter,
)

router = APIRouter(prefix="/models")
PROBE_TIMEOUT_SECONDS = 10.0


class ProbeError(Exception):
    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(code)


@router.get("")
async def list_models(
    request: Request,
    *,
    provider: str | None = None,
    enabled: str | None = None,
    source: str | None = None,
    capability: str | None = None,
    search: str | None = None,
    query: str | None = None,
    cursor: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    await require_admin(request)
    repository = _repository(request)
    selected_provider = provider_filter(provider)
    selected_search = _model_search(search, query)
    models = await repository.list_models(selected_provider)
    selected = _filter_models(
        models,
        enabled=bool_filter(enabled),
        source=text_filter(source, detail="invalid_source"),
        capability=text_filter(capability, detail="invalid_capability"),
        search=selected_search,
    )
    selected_limit = bounded_int(limit, default=100, maximum=100)
    page, next_cursor = page_slice(
        selected,
        cursor_value(cursor, allow_zero=True),
        selected_limit,
    )
    return {"models": page, "limit": selected_limit, "next_cursor": next_cursor}


@router.patch("/{provider}/{model_id}")
async def patch_model(provider: str, model_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    body = await json_object(request)
    if set(body) != {"enabled"} or not isinstance(body["enabled"], bool):
        raise HTTPException(status_code=400, detail="enabled_boolean_required")
    repository = _repository(request)
    async with repository.transaction():
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
    async with repository.transaction():
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


@router.post("/{provider}/{model_id}/probe")
async def probe_model(provider: str, model_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    if await json_object(request, allow_empty=True):
        raise HTTPException(status_code=400, detail="probe_body_not_allowed")
    state = admin_state(request)
    try:
        result = await probe_model_for_account(state, provider, None, model_id=model_id)
    except ProbeError as error:
        await _audit(
            request,
            "model.probe",
            provider,
            model_id,
            result="failed",
            metadata={"error_code": error.code},
        )
        raise HTTPException(status_code=error.status_code, detail=error.code) from error
    await _audit(request, "model.probe", provider, model_id)
    return result


async def probe_model_for_account(
    state: Any,
    provider: str,
    account_id: str | None,
    *,
    model_id: str | None = None,
) -> dict[str, Any]:
    if provider not in {"codebuddy", "qoder"}:
        raise ProbeError(400, "invalid_provider")
    selected_model = await _probe_model_id(state, provider, model_id)
    selected_account = account_id or _probe_account_id(state, provider)
    provider_client = None
    started = time.monotonic()
    try:
        provider_client = await _build_probe_provider(state, provider, selected_account)
        await asyncio.wait_for(
            provider_client.complete(_probe_request(selected_model)),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except TimeoutError as error:
        raise ProbeError(504, "probe_timeout") from error
    except LookupError as error:
        raise ProbeError(409, "account_not_probeable") from error
    except ProbeError:
        raise
    except Exception as error:
        raise ProbeError(502, "probe_failed") from error
    finally:
        if provider_client is not None:
            try:
                await provider_client.close()
            except Exception:
                pass
    return {
        "status": "succeeded",
        "provider": provider,
        "account_id": selected_account,
        "model_id": selected_model,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def _repository(request: Request):
    repository = getattr(admin_state(request), "account_repo", None)
    if repository is None:
        raise HTTPException(status_code=503, detail="repository_unavailable")
    return repository


async def _audit(
    request: Request,
    action: str,
    resource_type: str,
    resource_id: str,
    *,
    result: str = "succeeded",
    metadata: dict[str, Any] | None = None,
) -> None:
    repository = getattr(admin_state(request), "account_repo", None)
    if repository is not None:
        await repository.add_audit_event(
            actor_type="admin", actor_id=None, action=action,
            resource_type=resource_type, resource_id=resource_id, result=result,
            metadata=metadata,
        )


def _filter_models(
    models: list[dict[str, Any]],
    *,
    enabled: bool | None,
    source: str | None,
    capability: str | None,
    search: str | None,
) -> list[dict[str, Any]]:
    needle = search.casefold() if search is not None else None
    return [
        model
        for model in models
        if (enabled is None or model["enabled"] is enabled)
        and (source is None or model["source"] == source)
        and (capability is None or capability in model["capabilities"])
        and (
            needle is None
            or needle in model["model_id"].casefold()
            or needle in model["display_name"].casefold()
        )
    ]


def _model_search(search: str | None, query: str | None) -> str | None:
    if search is not None and query is not None and search != query:
        raise HTTPException(status_code=400, detail="conflicting_search")
    return text_filter(search or query, detail="invalid_search")


async def _probe_model_id(state: Any, provider: str, model_id: str | None) -> str:
    models = await state.account_repo.list_models(provider)
    if model_id is not None:
        match = next((item for item in models if item["model_id"] == model_id), None)
        if match is None:
            raise ProbeError(404, "model_not_found")
        if not match["enabled"]:
            raise ProbeError(409, "model_disabled")
        return model_id
    enabled = next((item["model_id"] for item in models if item["enabled"]), None)
    if enabled is not None:
        return enabled
    definitions = load_models_from_config(state.settings.model_config_path).get(provider, [])
    if not definitions:
        raise ProbeError(409, "provider_model_unavailable")
    return definitions[0].id


def _probe_account_id(state: Any, provider: str) -> str:
    account = next(
        (slot.account_id for slot in state.account_registry.snapshot("chat") if slot.provider == provider),
        None,
    )
    if account is None:
        raise ProbeError(409, "provider_account_unavailable")
    return account


async def _build_probe_provider(state: Any, provider: str, account_id: str):
    credential = await state.credential_resolver.credential(provider, account_id, "chat")
    factory = ProviderFactory(state.settings)
    if provider == "codebuddy":
        token = credential.payload.get("access_token") or credential.payload.get("token")
        if not token:
            raise LookupError("missing chat credential")
        return factory.codebuddy_static(token)
    token = credential.payload.get("pat") or credential.payload.get("access_token")
    if not token:
        raise LookupError("missing chat credential")
    return factory.qoder(token)


def _probe_request(model_id: str) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=model_id,
        messages=[
            ChatMessage(role="system", content="Health check."),
            ChatMessage(role="user", content="Reply OK."),
        ],
        stream=False,
        temperature=0,
        max_tokens=1,
    )
