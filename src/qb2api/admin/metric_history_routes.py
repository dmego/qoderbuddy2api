"""Admin metric history read API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .dependencies import admin_state, require_admin
from .observability_support import repository as _repository
from .validation import bounded_int, optional_account_id, provider_filter
from .views import find_account_view

router = APIRouter(tags=["admin"])


@router.get("/metrics/accounts/{provider}/{account_id}/history/{metric_kind}")
async def account_metric_history(
    provider: str,
    account_id: str,
    metric_kind: str,
    *,
    request: Request,
    limit: str | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    await require_admin(request)
    selected_provider = provider_filter(provider)
    selected_account = optional_account_id(account_id)
    state = admin_state(request)
    if find_account_view(state, selected_provider, selected_account) is None:
        raise HTTPException(status_code=404, detail="account_not_found")
    selected_limit = bounded_int(limit, default=500, maximum=2000)
    rows = await _repository(request).list_metric_history(
        provider=selected_provider,
        account_id=selected_account,
        metric_kind=metric_kind,
        limit=selected_limit,
        since=since or None,
    )
    return {
        "provider": selected_provider,
        "account_id": selected_account,
        "metric_kind": metric_kind,
        "rows": rows,
        "limit": selected_limit,
    }
