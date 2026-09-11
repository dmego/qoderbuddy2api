"""OrcaTerm account import and deletion routes.

OrcaTerm has no OAuth handshake of its own here: the credential is the desktop
app's OAuth JWT, which the operator copies from the local store
(``~/Library/Application Support/com.orcaterm-desktop.app/data.bin`` →
``oauth_access_token``). Import is therefore manual-only, and accounts are
chat-only (no sign-in / growth centre), mirroring the international WorkBuddy
wiring.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from qb2api.accounts.imports import persist_orcaterm_account

from .dependencies import admin_state, require_admin
from .import_support import orcaterm_identity
from .mutation_audit import refresh_after_mutation
from .validation import json_object, label, optional_account_id, required_string
from .views import account_view_dict, find_account_view

logger = logging.getLogger("qb2api.admin.orcaterm")

router = APIRouter()

PROVIDER = "orcaterm"

# Labels that mean "the operator did not pick a name"; replace them with the
# identity carried in the token so accounts stay recognizable.
_GENERIC_LABELS = frozenset({"orcaterm", "OrcaTerm", "OrcaTerm 桌面版"})


def _identity_or(selected: str, access_token: str | None) -> str:
    if selected not in _GENERIC_LABELS:
        return selected
    return (orcaterm_identity(access_token or "")[1]) or selected


@router.post("/auth/orcaterm/manual")
async def orcaterm_manual(request: Request) -> dict[str, Any]:
    """Import a pasted OrcaTerm desktop OAuth token.

    The desktop token expires after roughly two hours, so the operator
    re-imports (or rotates via the same endpoint) when the account reports
    ``expired``.
    """
    await require_admin(request)
    state = admin_state(request)
    body = await json_object(request)
    access_token = required_string(
        body, "token", "access_token", "bearer", detail="token_required"
    )
    refresh_token = _optional_string(body.get("refresh_token"))
    account_id = optional_account_id(body.get("account_id"))
    await _require_orcaterm_account(state, account_id)
    selected = label(body.get("label"), default="orcaterm")
    account_id = await persist_orcaterm_account(
        state.account_repo,
        state.credential_vault,
        label=_identity_or(selected, access_token),
        source="manual",
        access_token=access_token,
        refresh_token=refresh_token,
        account_id=account_id,
    )
    state.credential_resolver.invalidate(PROVIDER, account_id, "chat")
    return {
        "status": "ok",
        "account": await _publish(state, account_id, mutation_action="account.import"),
    }


@router.delete("/accounts/orcaterm/{account_id}")
async def orcaterm_delete(account_id: str, request: Request) -> dict[str, Any]:
    """Remove an OrcaTerm account and every credential attached to it."""
    await require_admin(request)
    state = admin_state(request)
    if state.account_registry.is_env_account(PROVIDER, account_id):
        raise HTTPException(status_code=400, detail="env_account_read_only")
    if find_account_view(state, PROVIDER, account_id) is None:
        raise HTTPException(status_code=404, detail="account_not_found")
    deleted = await state.account_repo.delete_account(PROVIDER, account_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="account_not_found")
    state.credential_resolver.invalidate(PROVIDER, account_id, "chat")
    await state.account_repo.add_audit_event(
        actor_type="admin",
        actor_id=None,
        action="account.delete",
        resource_type="account",
        resource_id=f"{PROVIDER}:{account_id}",
        result="succeeded",
    )
    await refresh_after_mutation(
        state,
        mutation_action="account.delete",
        resource_type="account",
        resource_id=f"{PROVIDER}:{account_id}",
    )
    return {"status": "deleted", "account_id": account_id}


async def _publish(state: Any, account_id: str, *, mutation_action: str) -> dict[str, Any]:
    await refresh_after_mutation(
        state,
        mutation_action=mutation_action,
        resource_type="account",
        resource_id=f"{PROVIDER}:{account_id}",
    )
    view = find_account_view(state, PROVIDER, account_id)
    if view is None:
        raise HTTPException(status_code=404, detail="account_not_found")
    return account_view_dict(view)


async def _require_orcaterm_account(state: Any, account_id: str | None) -> None:
    if account_id is None:
        return
    if state.account_registry.is_env_account(PROVIDER, account_id):
        raise HTTPException(status_code=400, detail="cannot_reauthorize_env_account")
    accounts = await state.account_repo.list_accounts(PROVIDER)
    if not any(account["account_id"] == account_id for account in accounts):
        raise HTTPException(status_code=404, detail="account_not_found")


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail="invalid_refresh_token")
    return value.strip()
