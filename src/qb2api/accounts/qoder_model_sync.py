"""Qoder upstream model discovery and catalog sync (Control-side only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ..providers.qoder_auth import QoderError

MODELS_ENDPOINT = "https://api.qoder.com.cn/api/v1/cloud/models"
REQUIRED_FIELDS = (
    "id",
    "display_name",
    "is_enabled",
    "is_new",
    "is_vl",
    "support_disable_reasoning",
    "price_factor",
    "max_input_tokens",
    "default_context_window",
    "available_context_windows",
    "efforts",
    "default_effort",
)


@dataclass(frozen=True, slots=True)
class UpstreamModel:
    id: str
    display_name: str
    is_enabled: bool
    is_new: bool = False
    is_vl: bool = False
    support_disable_reasoning: bool = False
    price_factor: float = 1.0
    max_input_tokens: int = 0
    default_context_window: int = 0
    available_context_windows: list[int] = field(default_factory=list)
    efforts: list[str] = field(default_factory=list)
    default_effort: str = ""

    @classmethod
    def from_dict(cls, item: dict[str, Any]) -> UpstreamModel:
        return cls(**{key: item[key] for key in REQUIRED_FIELDS if key in item})


@dataclass(frozen=True, slots=True)
class SyncReport:
    added: int = 0
    updated: int = 0
    disabled: int = 0
    models: list[dict[str, Any]] = field(default_factory=list)


async def fetch_qoder_models(
    pat: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[UpstreamModel]:
    """Fetch the official Qoder model list with a real PAT."""
    if client is None:
        async with httpx.AsyncClient(timeout=30) as owned:
            return await _fetch_once(owned, pat)
    return await _fetch_once(client, pat)


async def _fetch_once(client: httpx.AsyncClient, pat: str) -> list[UpstreamModel]:
    response = await client.get(
        MODELS_ENDPOINT,
        headers={"Authorization": f"Bearer {pat}"},
    )
    if not 200 <= response.status_code < 300:
        raise QoderError(
            f"Qoder models fetch failed (HTTP {response.status_code})",
            status_code=response.status_code,
        )
    payload = response.json()
    items = payload.get("data", []) if isinstance(payload, dict) else []
    return [UpstreamModel.from_dict(item) for item in items if isinstance(item, dict)]


def convert_upstream_models(items: list[UpstreamModel]) -> list[dict[str, Any]]:
    """Convert upstream models into `model_catalog` / snapshot-ready rows."""
    rows = []
    for item in items:
        capabilities = ["chat", "streaming"]
        if item.support_disable_reasoning:
            capabilities.append("reasoning")
        if item.default_effort:
            capabilities.append("reasoning_effort")
        if item.default_context_window:
            capabilities.append("context_window")
        rows.append(
            {
                "model_id": item.display_name,
                "display_name": item.display_name,
                "enabled": item.is_enabled,
                "capabilities": capabilities,
                "metadata": {
                    "cosy_key": item.id,
                    "is_new": item.is_new,
                    "is_vl": item.is_vl,
                    "price_factor": item.price_factor,
                    "max_input_tokens": item.max_input_tokens,
                    "default_context_window": item.default_context_window,
                    "available_context_windows": item.available_context_windows,
                    "efforts": item.efforts,
                    "default_effort": item.default_effort,
                    "source": "upstream",
                },
            }
        )
    return rows


async def _qoder_token(resolver, slot) -> str | None:
    """Resolve the PAT for one qoder account slot, or None when unavailable."""
    try:
        credential = await resolver.credential(slot.provider, slot.account_id, "chat")
    except LookupError:
        return None
    return credential.payload.get("pat") or credential.payload.get("access_token")


async def _pick_qoder_pat(registry, resolver) -> str | None:
    """Pick the first usable qoder chat PAT, preferring verified accounts."""
    slots = [slot for slot in registry.snapshot("chat") if slot.provider == "qoder"]
    ordered = [slot for slot in slots if slot.verification_status == "verified"]
    ordered.extend(slot for slot in slots if slot.verification_status != "verified")
    for slot in ordered:
        token = await _qoder_token(resolver, slot)
        if token:
            return token
    return None


async def sync_qoder_models(repository, registry, resolver, *, client=None) -> SyncReport:
    """Sync the catalog from upstream using one available qoder account PAT."""
    pat = await _pick_qoder_pat(registry, resolver)
    if pat is None:
        raise QoderError("No available qoder account credential", status_code=409)

    rows = convert_upstream_models(await fetch_qoder_models(pat, client=client))

    existing = await repository.list_models("qoder")
    baseline = {record["model_id"]: record for record in existing}

    added = 0
    updated = 0
    async with repository.transaction():
        for row in rows:
            added_delta, updated_delta = await _upsert_row(repository, row, baseline)
            added += added_delta
            updated += updated_delta
        disabled = await _disable_stale(repository, baseline, {row["model_id"] for row in rows})

    return SyncReport(added=added, updated=updated, disabled=disabled, models=rows)


async def _upsert_row(
    repository,
    row: dict[str, Any],
    baseline: dict[str, dict[str, Any]],
) -> tuple[int, int]:
    """Upsert one upstream row, returning its (added, updated) contribution counts."""
    previous = baseline.get(row["model_id"])
    if previous is None:
        deltas = (1, 0)
    elif _content_differs(previous, row):
        deltas = (0, 1)
    else:
        deltas = (0, 0)
    await repository.upsert_model(
        provider="qoder",
        model_id=row["model_id"],
        display_name=row["display_name"],
        capabilities=row["capabilities"],
        source="upstream",
        enabled=row["enabled"],
        metadata=row["metadata"],
    )
    return deltas


async def _disable_stale(
    repository,
    baseline: dict[str, dict[str, Any]],
    incoming_ids: set[str],
) -> int:
    """Disable enabled upstream rows missing from the incoming sync; return count."""
    disabled = 0
    for model_id, record in baseline.items():
        if (
            record.get("source") == "upstream"
            and model_id not in incoming_ids
            and record.get("enabled")
        ):
            await repository.set_model_enabled("qoder", model_id, False)
            disabled += 1
    return disabled


def _content_differs(previous: dict[str, Any], row: dict[str, Any]) -> bool:
    return (
        bool(previous.get("enabled")) != bool(row["enabled"])
        or list(previous.get("capabilities") or []) != list(row["capabilities"])
        or dict(previous.get("metadata") or {}) != dict(row["metadata"])
    )
