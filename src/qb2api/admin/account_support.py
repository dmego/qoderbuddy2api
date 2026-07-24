"""Small helpers shared by account administration routes."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request

from .mutation_audit import add_audit
from .validation import json_object
from .views import account_view_dict, find_account_view


def published_view(state: Any, provider: str, account_id: str) -> dict[str, Any]:
    view = find_account_view(state, provider, account_id)
    if view is None:
        raise HTTPException(status_code=404, detail="account_not_found")
    return account_view_dict(view)


def filter_accounts(
    accounts: list[dict[str, Any]],
    *,
    provider: str | None,
    source: str | None,
    status: str | None,
    purpose: str | None,
    query: str | None,
) -> list[dict[str, Any]]:
    needle = query.lower() if query else None
    return [
        account
        for account in accounts
        if (provider is None or account["provider"] == provider)
        and (source is None or account["source"] == source)
        and (status is None or account["summary_status"] == status)
        and (purpose is None or purpose in account["purposes"])
        and (
            needle is None
            or needle in f"{account['label']} {account['account_id']}".lower()
        )
    ]


async def empty_mutation_body(request: Request, detail: str) -> None:
    body = await json_object(request, allow_empty=True)
    if body:
        raise HTTPException(status_code=400, detail=detail)


async def account_audit(
    state: Any,
    action: str,
    *,
    provider: str,
    account_id: str,
    result: str = "succeeded",
) -> None:
    await add_audit(
        state.account_repo, action=action, resource_type="account",
        resource_id=f"{provider}:{account_id}", result=result,
    )
