"""Shared audit, pool-refresh, and worker-state helpers for catalog routes."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import Request

from .dependencies import admin_state

# The Worker answers this loopback-only probe; a slow or absent Worker must
# never block or fail an admin read, so the lookup is short and fails open.
_QUOTA_PROBE_TIMEOUT_S = 1.5


async def refresh_runtime(state: Any) -> None:
    refresh = getattr(state, "refresh_provider_pools", None)
    if refresh is not None:
        await refresh()


async def audit(
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


async def annotate_quota_blocks(state: Any, models: list[dict[str, Any]]) -> None:
    """Attach live per-account quota blocks from the Worker to each route.

    The Worker owns the block state (it is the process that observed the
    upstream quota error), so the admin view reads it back over the loopback
    internal API. A missing or unhealthy Worker leaves the rows unannotated
    rather than failing the request.
    """
    index = await quota_block_index(state)
    if not index:
        return
    for model in models:
        for route in model.get("routes") or []:
            blocked = index.get((route["provider"], route["upstream_id"])) or index.get(
                (route["provider"], model["model_id"])
            )
            if blocked:
                route["quota_blocked"] = blocked


async def quota_blocks(state: Any) -> list[dict[str, Any]]:
    """Raw live blocks reported by the Worker; empty when it is unreachable."""
    return await _fetch_quota_blocks(state)


async def quota_block_index(state: Any) -> dict[tuple[str, str], list[dict[str, str]]]:
    """Live (provider, model) → blocked accounts index; empty when unavailable."""
    payload = await _fetch_quota_blocks(state)
    index: dict[tuple[str, str], list[dict[str, str]]] = {}
    for block in payload:
        provider = str(block.get("provider") or "")
        model_id = str(block.get("model_id") or "")
        if not provider or not model_id:
            continue
        index.setdefault((provider, model_id), []).append({
            "account_id": str(block.get("account_id") or ""),
            "blocked_until": str(block.get("blocked_until") or ""),
        })
    return index


async def _fetch_quota_blocks(state: Any) -> list[dict[str, Any]]:
    settings = state.settings
    headers = {}
    if settings.worker_internal_token:
        headers["X-QB2API-Worker-Token"] = settings.worker_internal_token
    url = f"http://{settings.worker_host}:{settings.worker_port}/internal/quota-blocks"
    try:
        async with httpx.AsyncClient(
            timeout=_QUOTA_PROBE_TIMEOUT_S, trust_env=False
        ) as client:
            response = await client.get(url, headers=headers)
        payload = response.json()
    except Exception:
        return []
    blocks = payload.get("blocks") if isinstance(payload, dict) else None
    return [block for block in blocks or [] if isinstance(block, dict)]
