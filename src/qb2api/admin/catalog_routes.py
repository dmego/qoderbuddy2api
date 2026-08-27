"""Model catalog administration endpoints."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from qb2api.accounts.codebuddy_model_sync import sync_codebuddy_models
from qb2api.accounts.qoder_model_sync import sync_qoder_models
from qb2api.models import ModelCapabilities, ModelDefinition, load_models_from_config, load_unified_overrides
from qb2api.models_catalog import UnifiedModel, build_unified_catalog
from qb2api.openai import ChatCompletionRequest, ChatMessage
from qb2api.provider_factory import ProviderFactory
from qb2api.providers.qoder_auth import QoderError

from .catalog_filters import filter_models
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
    selected_provider = provider_filter(provider)
    selected_search = _model_search(search, query)
    models = await _unified_models(admin_state(request))
    selected = filter_models(
        models,
        enabled=bool_filter(enabled),
        source=text_filter(source, detail="invalid_source"),
        capability=text_filter(capability, detail="invalid_capability"),
        search=selected_search,
        provider=selected_provider,
    )
    selected_limit = bounded_int(limit, default=100, maximum=100)
    page, next_cursor = page_slice(
        selected,
        cursor_value(cursor, allow_zero=True),
        selected_limit,
    )
    return {"models": page, "limit": selected_limit, "next_cursor": next_cursor}


async def _unified_models(state: Any) -> list[dict[str, Any]]:
    """Admin-facing unified catalog view with per-route enabled state."""
    settings = state.settings
    per_provider = load_models_from_config(settings.model_config_path)
    repository = state.account_repo
    route_enabled: dict[tuple[str, str], bool] = {}
    if repository is not None:
        rows = await repository.list_models()
        for row in rows:
            route_enabled[(row["provider"], row["model_id"])] = bool(row["enabled"])
        upstream = [
            _catalog_row_definition(row)
            for row in await repository.list_models("qoder")
            if row.get("source") == "upstream"
        ]
        per_provider["qoder"] = upstream
    overrides = load_unified_overrides(settings.model_config_path)
    catalog = build_unified_catalog(per_provider, overrides)
    return [_unified_row(entry, route_enabled) for entry in catalog.values()]


def _catalog_row_definition(row: dict[str, Any]) -> ModelDefinition:
    capabilities = row.get("capabilities") or []
    metadata = row.get("metadata") or {}
    return ModelDefinition(
        id=row["model_id"],
        name=row.get("display_name") or row["model_id"],
        provider="qoder",
        capabilities=ModelCapabilities(
            **{
                name: name in capabilities
                for name in (
                    "chat", "streaming", "tool_calling", "reasoning",
                    "reasoning_effort", "context_window", "max_output_tokens",
                )
            }
        ),
        max_context=int(metadata.get("default_context_window") or 0) or 128000,
        max_output=4096,
        metadata={"cosy_key": metadata.get("cosy_key")},
    )


def _unified_row(
    entry: UnifiedModel,
    route_enabled: dict[tuple[str, str], bool],
) -> dict[str, Any]:
    routes = []
    for route in entry.routes:
        enabled = route_enabled.get((route.provider, route.upstream_id), True)
        routes.append({
            "provider": route.provider,
            "upstream_id": route.upstream_id,
            "enabled": enabled,
            "source": "upstream" if route.provider == "qoder" else "definition",
        })
    return {
        "model_id": entry.id,
        "display_name": entry.name,
        "capabilities": _capability_names(entry.capabilities),
        "enabled": any(route["enabled"] for route in routes),
        "source": "upstream" if any(route["provider"] == "qoder" for route in routes) else "definition",
        "routes": routes,
        "last_seen_at": None,
    }


def _capability_names(capabilities: ModelCapabilities) -> list[str]:
    return [
        name
        for name, flag in (
            ("chat", capabilities.chat),
            ("streaming", capabilities.streaming),
            ("tool_calling", capabilities.tool_calling),
            ("reasoning", capabilities.reasoning),
            ("reasoning_effort", capabilities.reasoning_effort),
            ("context_window", capabilities.context_window),
            ("max_output_tokens", capabilities.max_output_tokens),
        )
        if flag
    ]


@router.patch("/{model_id}")
async def patch_unified_model(model_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    body = await json_object(request)
    if set(body) != {"enabled"} or not isinstance(body["enabled"], bool):
        raise HTTPException(status_code=400, detail="enabled_boolean_required")
    state = admin_state(request)
    repository = _repository(request)
    entry = await _unified_entry(state, model_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="model_not_found")
    async with repository.transaction():
        for route in entry["routes"]:
            if route["provider"] == "codebuddy":
                await repository.upsert_model(
                    provider="codebuddy",
                    model_id=route["upstream_id"],
                    display_name=entry["display_name"],
                    capabilities=entry["capabilities"],
                    source="definition",
                    enabled=body["enabled"],
                )
            else:
                await repository.set_model_enabled("qoder", route["upstream_id"], body["enabled"])
        await _audit(
            request,
            action="model.update",
            resource_type="unified",
            resource_id=model_id,
        )
    await _refresh_runtime(state)
    return {"model_id": model_id, "enabled": body["enabled"]}


@router.post("/{model_id}/probe")
async def probe_unified_model(model_id: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    if await json_object(request, allow_empty=True):
        raise HTTPException(status_code=400, detail="probe_body_not_allowed")
    state = admin_state(request)
    entry = await _unified_entry(state, model_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="model_not_found")
    routes = [route for route in entry["routes"] if route["enabled"]]
    if not routes:
        raise HTTPException(status_code=409, detail="model_disabled")
    results = await _probe_routes(state, routes)
    succeeded = all(result["status"] == "succeeded" for result in results)
    await _audit(
        request,
        action="model.probe",
        resource_type="unified",
        resource_id=model_id,
        result="succeeded" if succeeded else "failed",
    )
    return {"status": "succeeded" if succeeded else "failed", "model_id": model_id, "routes": results}


async def _unified_entry(state: Any, model_id: str) -> dict[str, Any] | None:
    return next(
        (model for model in await _unified_models(state) if model["model_id"] == model_id),
        None,
    )


async def _probe_routes(state: Any, routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for route in routes:
        try:
            result = await probe_model_for_account(
                state, route["provider"], None, model_id=route["upstream_id"]
            )
            results.append({
                "provider": route["provider"],
                "upstream_id": route["upstream_id"],
                "status": "succeeded",
                "latency_ms": result["latency_ms"],
            })
        except ProbeError as error:
            results.append({
                "provider": route["provider"],
                "upstream_id": route["upstream_id"],
                "status": "failed",
                "error_code": error.code,
            })
    return results


async def _refresh_runtime(state: Any) -> None:
    refresh = getattr(state, "refresh_provider_pools", None)
    if refresh is not None:
        await refresh()


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
        await _audit(
            request,
            action="model.update",
            resource_type=provider,
            resource_id=model_id,
        )
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
        await _audit(
            request,
            action="model.refresh",
            resource_type="catalog",
            resource_id="catalog",
        )
    return {"status": "succeeded", "refreshed": count}


@router.post("/sync/{provider}")
async def sync_upstream_models(provider: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    if provider == "qoder":
        try:
            report = await sync_qoder_models(
                state.account_repo,
                state.account_registry,
                state.credential_resolver,
            )
        except QoderError as error:
            await _audit(
                request,
                action="model.sync",
                resource_type=provider,
                resource_id="catalog",
                result="failed",
                metadata={"error_code": error.status_code},
            )
            raise HTTPException(status_code=error.status_code, detail="sync_failed") from error
        await _audit(
            request,
            action="model.sync",
            resource_type=provider,
            resource_id="catalog",
            metadata={"added": report.added, "updated": report.updated, "disabled": report.disabled},
        )
        return {
            "status": "succeeded",
            "added": report.added,
            "updated": report.updated,
            "disabled": report.disabled,
            "models": report.models,
        }
    if provider == "codebuddy":
        try:
            report = await sync_codebuddy_models(
                state.account_repo,
                state.account_registry,
                state.credential_resolver,
                models_config_path=state.settings.model_config_path,
            )
        except Exception as error:
            await _audit(
                request,
                action="model.sync",
                resource_type=provider,
                resource_id="catalog",
                result="failed",
                metadata={"error": type(error).__name__},
            )
            raise HTTPException(status_code=502, detail="sync_failed") from error
        await _audit(
            request,
            action="model.sync",
            resource_type=provider,
            resource_id="catalog",
            metadata={"added": report.added, "updated": report.updated, "removed": report.removed},
        )
        return {
            "status": "succeeded",
            "added": report.added,
            "updated": report.updated,
            "removed": report.removed,
            "probed": report.probed,
            "models": report.models,
        }
    raise HTTPException(status_code=400, detail="unsupported_provider")


@router.post("/sync")
async def sync_all_models(request: Request) -> dict[str, Any]:
    """全量上游同步：qoder 官方目录 + workbuddy 探测，各自容错、错误不阻断。"""
    await require_admin(request)
    state = admin_state(request)
    result: dict[str, Any] = {"status": "succeeded", "providers": {}}
    totals = {"added": 0, "updated": 0, "removed": 0, "disabled": 0}
    try:
        report = await sync_qoder_models(
            state.account_repo,
            state.account_registry,
            state.credential_resolver,
        )
        result["providers"]["qoder"] = {
            "status": "succeeded",
            "added": report.added,
            "updated": report.updated,
            "disabled": report.disabled,
        }
        totals["added"] += report.added
        totals["updated"] += report.updated
        totals["disabled"] += report.disabled
    except Exception as error:
        result["providers"]["qoder"] = {"status": "failed", "error": type(error).__name__}
    try:
        report = await sync_codebuddy_models(
            state.account_repo,
            state.account_registry,
            state.credential_resolver,
            models_config_path=state.settings.model_config_path,
        )
        result["providers"]["codebuddy"] = {
            "status": "succeeded",
            "added": report.added,
            "updated": report.updated,
            "removed": report.removed,
            "probed": report.probed,
        }
        totals["added"] += report.added
        totals["updated"] += report.updated
        totals["removed"] += report.removed
    except Exception as error:
        result["providers"]["codebuddy"] = {"status": "failed", "error": type(error).__name__}
    await _audit(
        request,
        action="model.sync",
        resource_type="catalog",
        resource_id="catalog",
        result=result["status"],
        metadata={
            "qoder": result["providers"].get("qoder", {}).get("status"),
            "codebuddy": result["providers"].get("codebuddy", {}).get("status"),
        },
    )
    result.update(totals)
    return result


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
            action="model.probe",
            resource_type=provider,
            resource_id=model_id,
            result="failed",
            metadata={"error_code": error.code},
        )
        raise HTTPException(status_code=error.status_code, detail=error.code) from error
    await _audit(
        request,
        action="model.probe",
        resource_type=provider,
        resource_id=model_id,
    )
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
    *,
    action: str,
    resource_type: str,
    resource_id: str,
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
    return await _fallback_probe_model(state, provider, models)


async def _fallback_probe_model(state: Any, provider: str, models: list[dict[str, Any]]) -> str:
    enabled = next((item["model_id"] for item in models if item["enabled"]), None)
    if enabled is not None:
        return enabled
    if provider != "codebuddy":
        raise ProbeError(409, "provider_model_unavailable")
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
