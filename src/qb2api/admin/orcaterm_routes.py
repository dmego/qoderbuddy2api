"""OrcaTerm account import and deletion routes.

Two import paths: browser login against the console (the operator signs in with
Tencent Cloud and the console hands the browser back to us with a session id we
exchange for the token), and manual paste of the desktop app's OAuth JWT from
``~/Library/Application Support/com.orcaterm-desktop.app/data.bin`` →
``oauth_access_token``. Accounts are chat-only (no sign-in / growth centre),
mirroring the international WorkBuddy wiring.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from qb2api.accounts.imports import persist_orcaterm_account
from qb2api.auth.flows import FlowBusyError
from qb2api.auth.orcaterm import build_authorize_url

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


@router.post("/auth/orcaterm/start")
async def orcaterm_oauth_start(request: Request) -> dict[str, Any]:
    """Begin a browser login: return the console authorize URL to open."""
    await require_admin(request)
    state = admin_state(request)
    if not state.settings.orcaterm_oauth_enabled:
        raise HTTPException(status_code=400, detail="oauth_disabled")
    body = await json_object(request, allow_empty=True)
    account_id = optional_account_id(body.get("account_id"))
    await _require_orcaterm_account(state, account_id)
    selected_label = label(body.get("label"), default="orcaterm")
    # The console binds the login to our session id on the tab it opened; the
    # admin page keeps polling /poll with that id until the exchange succeeds.
    return_url = _optional_string(body.get("return_url")) or _default_return_url(request)
    started = build_authorize_url(return_url=return_url)
    flow = state.oauth_flows.create(
        label=selected_label,
        auth_state=started.session_id,
        auth_url=started.auth_url,
        account_id=account_id,
    )
    return {
        "flow_id": flow.flow_id,
        "auth_url": flow.auth_url,
        "expires_at": _iso(flow.expires_at),
        "label": flow.label,
        "account_id": flow.account_id,
        "session_id": started.session_id,
    }


@router.post("/auth/orcaterm/poll")
async def orcaterm_oauth_poll(request: Request) -> Any:
    """Exchange a completed login session for credentials and persist them.

    ``session_id`` comes from the URL the browser landed on. It either matches
    the session this flow started with, or — when the callback lands in a fresh
    tab that never saw ``flow_id`` — identifies the flow on its own, so a stray
    id still cannot import someone else's login.
    """
    await require_admin(request)
    state = admin_state(request)
    body = await json_object(request)
    session_id = _optional_string(body.get("session_id"))
    flow_id = _optional_string(body.get("flow_id"))
    if flow_id is None:
        if session_id is None:
            raise HTTPException(status_code=400, detail="flow_id_required")
        flow_id = state.oauth_flows.find_by_state(session_id)
        if flow_id is None:
            raise HTTPException(status_code=404, detail="flow_not_found_or_expired")
    try:
        lease = state.oauth_flows.begin_poll(flow_id)
    except FlowBusyError:
        return JSONResponse(status_code=409, content={"error": "flow_poll_in_progress"})
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    consume = False
    try:
        if session_id and session_id != lease.auth_state:
            raise HTTPException(status_code=400, detail="session_id_mismatch")
        result = await state.orcaterm_oauth.exchange(lease.auth_state)
        if result.status != "success" or not result.access_token:
            # A not-yet-finished login reports expired/unknown; keep the flow
            # alive so the operator can finish signing in and poll again. The
            # deadline travels back so a caller that never saw /start (the
            # console callback lands in a fresh tab) still stops polling.
            return {
                "status": "pending",
                "message": result.message or "auth_pending",
                "expires_at": _iso(lease.record.expires_at),
            }
        account_id = await _persist_result(state, lease.record, result)
        consume = True
        return {
            "status": "success",
            "account": await _publish(state, account_id, mutation_action="account.import"),
        }
    finally:
        state.oauth_flows.finish_poll(flow_id, consume=consume)


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
    refresh_token = _optional_string(body.get("refresh_token"), detail="invalid_refresh_token")
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


async def _persist_result(state: Any, record: Any, result: Any) -> str:
    """Persist an exchanged browser-login token, replacing the same user."""
    expires_at = _expires_at(result.expires_in)
    account_id = await persist_orcaterm_account(
        state.account_repo,
        state.credential_vault,
        label=_identity_or(record.label, result.access_token),
        source="oauth",
        access_token=result.access_token,
        expires_at=expires_at,
        account_id=record.account_id,
    )
    state.credential_resolver.invalidate(PROVIDER, account_id, "chat")
    return account_id


def _default_return_url(request: Request) -> str:
    """Where the console parks the browser once the Tencent Cloud login ends.

    Targets the add-account page (a real SPA route that already understands
    ``provider``), so the operator lands back where the import panel is
    mounted. The console does not carry ``session_id`` through this redirect;
    the session stays bound server-side and the panel polls it from /start's
    reply. Derived from the request so the flow also works behind a reverse
    proxy or on a LAN address.
    """
    origin = str(request.base_url).rstrip("/")
    return f"{origin}/admin/accounts/add?provider=orcaterm"


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).replace(microsecond=0).isoformat()


def _expires_at(expires_in: int | None) -> str | None:
    if not isinstance(expires_in, int) or expires_in <= 0:
        return None
    return (datetime.now(UTC) + timedelta(seconds=expires_in)).replace(microsecond=0).isoformat()


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


def _optional_string(value: Any, *, detail: str = "invalid_value") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=detail)
    return value.strip()
