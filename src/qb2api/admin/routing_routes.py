"""Per-model provider routing policy administration.

The administrator sets, for one unified model id, which provider is tried first
(``priority``), how the load is shared inside one tier (``weight``), and whether
a route participates at all (``enabled``). The Control Plane stores the policy
and ships it to the Worker inside the runtime snapshot.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from qb2api.models import load_models_from_config, load_unified_overrides
from qb2api.models_catalog import build_unified_catalog
from qb2api.route_policy import (
    RoutePolicy,
    default_policies,
    policies_to_payload,
    validate_policies,
)

from .catalog_support import quota_blocks
from .dependencies import admin_state, require_admin
from .mutation_audit import add_audit, refresh_after_mutation
from .validation import json_object

router = APIRouter()

MAX_MODEL_ID = 256


@router.get("/routing")
async def list_routing(request: Request) -> dict[str, Any]:
    """Every unified model with its routes, accounts, and block state."""
    await require_admin(request)
    state = admin_state(request)
    stored = await _stored_policies(state)
    blocks = await _block_index(state)
    accounts = await _accounts_by_provider(state)
    models = await _catalog_models(state)
    return {
        "models": [
            {
                "model_id": model_id,
                "name": entry["name"],
                "routes": [
                    _route_view(
                        policy,
                        entry["capabilities"],
                        {"accounts": accounts, "blocks": blocks, "model_id": model_id},
                    )
                    for policy in _effective_policies(model_id, entry["providers"], stored)
                ],
                "configured": model_id in stored,
            }
            for model_id, entry in models
        ]
    }


@router.put("/routing/{model_id}/accounts/{provider}/{account_id}")
async def set_account_block(
    request: Request,
    *,
    model_id: str,
    provider: str,
    account_id: str,
) -> dict[str, Any]:
    """Block or unblock one account for one model.

    ``{"blocked": true}`` records a manual exclusion (with an optional reason);
    ``{"blocked": false}`` clears any block, manual or auto-detected.
    """
    await require_admin(request)
    state = admin_state(request)
    _validate_block_target(model_id, provider, account_id)
    body = await json_object(request)
    blocked = body.get("blocked")
    if not isinstance(blocked, bool):
        raise HTTPException(status_code=400, detail="blocked_boolean_required")
    reason = str(body.get("reason") or "").strip()[:200]
    if blocked:
        await state.account_repo.set_account_model_block(
            provider=provider,
            account_id=account_id,
            model_id=model_id,
            reason=reason or "管理员手动停用",
            source="manual",
        )
    else:
        await state.account_repo.clear_account_model_block(
            provider=provider, account_id=account_id, model_id=model_id
        )
    await add_audit(
        state.account_repo,
        action="routing.account_block" if blocked else "routing.account_unblock",
        resource_type="account",
        resource_id=f"{provider}:{account_id}:{model_id}",
    )
    await refresh_after_mutation(
        state,
        mutation_action="routing.account_block",
        resource_type="account",
        resource_id=f"{provider}:{account_id}",
    )
    return {"status": "ok", "blocked": blocked, "model_id": model_id,
            "provider": provider, "account_id": account_id}


def _validate_block_target(model_id: str, provider: str, account_id: str) -> None:
    if not model_id or len(model_id) > MAX_MODEL_ID:
        raise HTTPException(status_code=400, detail="invalid_model_id")
    if not provider or len(provider) > 64:
        raise HTTPException(status_code=400, detail="invalid_provider")
    if not account_id or len(account_id) > 64:
        raise HTTPException(status_code=400, detail="invalid_account_id")


@router.put("/routing/{model_id}")
async def set_routing(model_id: str, request: Request) -> dict[str, Any]:
    """Replace the whole route policy for one model."""
    await require_admin(request)
    state = admin_state(request)
    if not model_id or len(model_id) > MAX_MODEL_ID:
        raise HTTPException(status_code=400, detail="invalid_model_id")
    body = await json_object(request)
    routes = body.get("routes")
    models = dict(await _catalog_models(state))
    entry = models.get(model_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="model_not_found")
    providers = tuple(entry["providers"])
    try:
        policies = validate_policies(routes, providers)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    await state.account_repo.replace_route_policies(
        model_id, policies_to_payload(policies)
    )
    await add_audit(
        state.account_repo,
        action="routing.update",
        resource_type="model",
        resource_id=model_id,
    )
    await refresh_after_mutation(
        state,
        mutation_action="routing.update",
        resource_type="model",
        resource_id=model_id,
    )
    return {
        "status": "ok",
        "model_id": model_id,
        "routes": [
            _route_view(policy, entry["capabilities"])
            for policy in _effective_policies(model_id, providers, {model_id: policies})
        ],
    }


@router.delete("/routing/{model_id}")
async def reset_routing(model_id: str, request: Request) -> dict[str, Any]:
    """Drop a stored policy so the model returns to default round-robin."""
    await require_admin(request)
    state = admin_state(request)
    deleted = await state.account_repo.delete_route_policies(model_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="routing_policy_not_found")
    await add_audit(
        state.account_repo,
        action="routing.reset",
        resource_type="model",
        resource_id=model_id,
    )
    await refresh_after_mutation(
        state,
        mutation_action="routing.reset",
        resource_type="model",
        resource_id=model_id,
    )
    return {"status": "reset", "model_id": model_id}


async def _catalog_models(state: Any) -> list[tuple[str, dict[str, Any]]]:
    """Unified catalog entries that have more than zero routes."""
    settings = state.settings
    per_provider = load_models_from_config(settings.model_config_path)
    repository = state.account_repo
    if repository is not None:
        from qb2api.admin.catalog_routes import upstream_model_definitions

        per_provider["qoder"] = await upstream_model_definitions(state)
    catalog = build_unified_catalog(
        per_provider, load_unified_overrides(settings.model_config_path)
    )
    return [
        (
            model_id,
            {
                "name": entry.name,
                "providers": tuple(route.provider for route in entry.routes),
                "capabilities": entry.capabilities,
            },
        )
        for model_id, entry in sorted(catalog.items())
        if entry.routes
    ]


async def _stored_policies(state: Any) -> dict[str, dict[str, RoutePolicy]]:
    repository = state.account_repo
    if repository is None:
        return {}
    stored: dict[str, dict[str, RoutePolicy]] = {}
    for row in await repository.list_route_policies():
        stored.setdefault(str(row["model_id"]), {})[str(row["provider"])] = RoutePolicy(
            provider=str(row["provider"]),
            priority=int(row["priority"]),
            weight=int(row["weight"]),
            enabled=bool(row["enabled"]),
        )
    return stored


def _effective_policies(
    model_id: str,
    providers: tuple[str, ...],
    stored: dict[str, dict[str, RoutePolicy]],
) -> list[RoutePolicy]:
    """Stored policy when present, otherwise the equal-weight default tier."""
    saved = stored.get(model_id)
    if not saved:
        return list(default_policies(providers).values())
    merged = default_policies(providers)
    for provider, policy in saved.items():
        if provider in merged:
            merged[provider] = policy
    return list(merged.values())


async def _accounts_by_provider(state: Any) -> dict[str, list[dict[str, str]]]:
    """Chat-capable accounts per provider, for the per-account block grid."""
    registry = getattr(state, "account_registry", None)
    if registry is None:
        return {}
    labels = {
        f"{view.provider}:{view.account_id}": view.label
        for view in registry.list_views()
    }
    grouped: dict[str, list[dict[str, str]]] = {}
    for slot in registry.snapshot("chat"):
        grouped.setdefault(slot.provider, []).append({
            "account_id": slot.account_id,
            "label": labels.get(f"{slot.provider}:{slot.account_id}") or slot.account_id,
            "source": slot.source,
        })
    return grouped


async def _block_index(state: Any) -> dict[tuple[str, str, str], dict[str, str]]:
    """(provider, account, model) → block info, from storage plus live Worker state.

    Stored rows win: a manual block is authoritative even if the Worker has not
    picked up the new snapshot yet.
    """
    index = await _stored_block_index(state)
    for live in await quota_blocks(state):
        key = _block_key(live)
        if key is not None:
            index.setdefault(key, _block_info(live, default_source="auto"))
    return index


async def _stored_block_index(state: Any) -> dict[tuple[str, str, str], dict[str, str]]:
    repository = state.account_repo
    if repository is None:
        return {}
    return {
        (str(row["provider"]), str(row["account_id"]), str(row["model_id"])): _block_info(
            row, default_source="manual"
        )
        for row in await repository.list_account_model_blocks()
    }


def _block_key(block: dict[str, Any]) -> tuple[str, str, str] | None:
    key = (
        str(block.get("provider") or ""),
        str(block.get("account_id") or ""),
        str(block.get("model_id") or ""),
    )
    return key if all(key) else None


def _block_info(block: dict[str, Any], *, default_source: str) -> dict[str, str]:
    return {
        "source": str(block.get("source") or default_source),
        "reason": str(block.get("reason") or ""),
        "blocked_until": str(block.get("blocked_until") or ""),
    }


def _route_view(
    policy: RoutePolicy,
    capabilities: Any,
    context: dict[str, Any],
) -> dict[str, Any]:
    accounts = context["accounts"].get(policy.provider, [])
    blocks = context["blocks"]
    model_id = context["model_id"]
    return {
        "provider": policy.provider,
        "priority": policy.priority,
        "weight": policy.weight,
        "enabled": policy.enabled,
        "capabilities": _capabilities(capabilities),
        "accounts": [
            {
                **account,
                "blocked": (policy.provider, account["account_id"], model_id) in blocks,
                "block": blocks.get((policy.provider, account["account_id"], model_id)),
            }
            for account in accounts
        ],
    }


def _capabilities(value: Any) -> list[str]:
    names = (
        "chat", "streaming", "tool_calling", "reasoning",
        "reasoning_effort", "context_window", "max_output_tokens",
    )
    return [name for name in names if getattr(value, name, False)]
