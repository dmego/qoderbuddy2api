"""Upstream model catalog synchronisation endpoints.

Domestic CodeBuddy discovers models by probing the chat endpoint, Qoder reads
its official catalog, and the international WorkBuddy pool is a fixed free
tier — so it is never probed and never rewritten from upstream.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from qb2api.accounts.codebuddy_model_sync import sync_codebuddy_models
from qb2api.accounts.qoder_model_sync import sync_qoder_models
from qb2api.providers.qoder_auth import QoderError

from .catalog_support import audit, refresh_runtime
from .dependencies import admin_state, require_admin

router = APIRouter(prefix="/models")


class SyncFailure(Exception):
    """A provider sync failed; carries the public status and error label."""

    def __init__(self, *, status_code: int, error_name: str) -> None:
        self.status_code = status_code
        self.error_name = error_name
        super().__init__(error_name)


@router.post("/sync/{provider}")
async def sync_upstream_models(provider: str, request: Request) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    runners = {"qoder": _sync_qoder, "codebuddy": _sync_codebuddy}
    runner = runners.get(provider)
    if runner is None:
        raise HTTPException(status_code=400, detail="unsupported_provider")
    try:
        return await runner(request, state)
    except SyncFailure as failure:
        raise HTTPException(
            status_code=failure.status_code, detail="sync_failed"
        ) from failure


@router.post("/sync")
async def sync_all_models(request: Request) -> dict[str, Any]:
    """全量上游同步：qoder 官方目录 + codebuddy 探测，各自容错、错误不阻断。

    国际版 WorkBuddy 只使用固定的三个免费模型，既不做上游探测也不改写目录。
    """
    await require_admin(request)
    state = admin_state(request)
    providers: dict[str, Any] = {}
    totals = {"added": 0, "updated": 0, "removed": 0, "disabled": 0}
    for provider, runner, fields in (
        ("qoder", _sync_qoder, ("added", "updated", "disabled")),
        ("codebuddy", _sync_codebuddy, ("added", "updated", "removed")),
    ):
        try:
            report = await runner(request, state)
        except SyncFailure as failure:
            providers[provider] = {"status": "failed", "error": failure.error_name}
            continue
        providers[provider] = {"status": "succeeded"} | {
            field: report[field] for field in fields
        }
        if provider == "codebuddy":
            providers[provider]["probed"] = report["probed"]
        for field in fields:
            totals[field] += report[field]
    await audit(
        request,
        action="model.sync",
        resource_type="catalog",
        resource_id="catalog",
        metadata={name: entry.get("status") for name, entry in providers.items()},
    )
    return {"status": "succeeded", "providers": providers} | totals


async def _sync_qoder(request: Request, state: Any) -> dict[str, Any]:
    try:
        report = await sync_qoder_models(
            state.account_repo, state.account_registry, state.credential_resolver
        )
    except QoderError as error:
        await _sync_failed(request, "qoder", error.status_code)
        raise SyncFailure(
            status_code=error.status_code, error_name=type(error).__name__
        ) from error
    await audit(
        request,
        action="model.sync",
        resource_type="qoder",
        resource_id="catalog",
        metadata={"added": report.added, "updated": report.updated, "disabled": report.disabled},
    )
    await refresh_runtime(state)
    return {
        "status": "succeeded",
        "added": report.added,
        "updated": report.updated,
        "disabled": report.disabled,
        "models": report.models,
    }


async def _sync_codebuddy(request: Request, state: Any) -> dict[str, Any]:
    try:
        report = await sync_codebuddy_models(
            state.account_repo,
            state.account_registry,
            state.credential_resolver,
            models_config_path=state.settings.model_config_path,
        )
    except Exception as error:
        await _sync_failed(request, "codebuddy", type(error).__name__)
        raise SyncFailure(status_code=502, error_name=type(error).__name__) from error
    await audit(
        request,
        action="model.sync",
        resource_type="codebuddy",
        resource_id="catalog",
        metadata={"added": report.added, "updated": report.updated, "removed": report.removed},
    )
    await refresh_runtime(state)
    return {
        "status": "succeeded",
        "added": report.added,
        "updated": report.updated,
        "removed": report.removed,
        "probed": report.probed,
        "models": report.models,
    }


async def _sync_failed(request: Request, provider: str, error_code: Any) -> None:
    await audit(
        request,
        action="model.sync",
        resource_type=provider,
        resource_id="catalog",
        result="failed",
        metadata={"error_code": error_code},
    )
