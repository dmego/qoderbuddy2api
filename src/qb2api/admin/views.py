"""Secret-free admin response helpers."""

from __future__ import annotations

from typing import Any


def account_view_dict(view: Any) -> dict[str, Any]:
    return {
        "provider": view.provider,
        "account_id": view.account_id,
        "label": view.label,
        "source": view.source,
        "enabled": view.enabled,
        "summary_status": view.summary_status,
        "purposes": view.purposes,
        "masked_identity": view.masked_identity,
        "shadowed": view.shadowed,
        "created_at": view.created_at,
        "updated_at": view.updated_at,
    }


def find_account_view(state: Any, provider: str, account_id: str) -> Any | None:
    return next(
        (
            view
            for view in state.account_registry.list_views()
            if view.provider == provider and view.account_id == account_id
        ),
        None,
    )
