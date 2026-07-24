"""End-to-end contracts for Task 5 account onboarding workflows."""

from __future__ import annotations

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from qb2api.accounts.imports import (
    persist_codebuddy_account,
    persist_codebuddy_checkin,
    persist_qoder_chat,
)
from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.accounts.vault import CredentialVault
from qb2api.admin.auth import AdminSessionStore
from qb2api.admin.router import router as admin_router
from qb2api.auth.codebuddy_oauth import OAuthPollResult, OAuthStartResult
from qb2api.auth.flows import FlowStore
from qb2api.checkin.models import CheckInOutcome, CheckInResult
from qb2api.config import Settings


class _WorkBuddyProbe:
    def __init__(self, outcome: CheckInOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []
        self.closed = False

    async def checkin(self, **values: object) -> CheckInResult:
        self.calls.append(values)
        return CheckInResult(outcome=self.outcome, provider="codebuddy")

    async def close(self) -> None:
        self.closed = True


class _OAuthSuccess:
    async def start(self) -> OAuthStartResult:
        return OAuthStartResult(auth_state="raw-auth-state", auth_url="https://example.test/authorize")

    async def poll(self, _state: str) -> OAuthPollResult:
        return OAuthPollResult(
            status="success",
            access_token="rotated-access-token",
            refresh_token="rotated-refresh-token",
            expires_in=3600,
        )


@pytest.fixture
async def onboarding_context(tmp_path):
    repository = AccountRepository(str(tmp_path / "accounts.sqlite3"))
    await repository.connect()
    await repository.migrate()
    vault = CredentialVault(Fernet.generate_key().decode())
    registry = AccountRegistry(repository, vault)
    await registry.rebuild()
    app = FastAPI()
    app.include_router(admin_router)
    app.state.settings = Settings(admin_key="admin-secret")
    app.state.account_repo = repository
    app.state.credential_vault = vault
    app.state.account_registry = registry
    app.state.credential_resolver = CredentialResolver(repository, vault, registry)
    app.state.admin_sessions = AdminSessionStore()
    app.state.oauth_flows = FlowStore()

    async def refresh() -> None:
        await registry.rebuild()

    app.state.refresh_provider_pools = refresh
    yield app, repository, vault, registry
    await repository.close()


@pytest.mark.asyncio
async def test_workbuddy_cookie_import_validates_before_persisting(onboarding_context) -> None:
    app, repository, vault, registry = onboarding_context
    account_id = await persist_codebuddy_account(
        repository, vault, label="cookie", source="manual", access_token="chat-secret"
    )
    await registry.rebuild()
    probe = _WorkBuddyProbe(CheckInOutcome.ALREADY_CHECKED_IN)
    app.state.workbuddy_client_factory = lambda: probe

    response = await _post(
        app,
        "/api/admin/auth/codebuddy/checkin",
        {"account_id": account_id, "mode": "cookie", "cookie": "session=secret"},
    )

    assert response.status_code == 200
    assert "session=secret" not in response.text
    assert probe.closed is True
    assert probe.calls[0]["auth_mode"] == "cookie"
    credential = await repository.get_credential("codebuddy", account_id, "checkin")
    assert credential is not None
    assert credential["mode"] == "cookie"
    assert vault.decrypt(credential["encrypted_payload"])["cookie"] == "session=secret"
    purpose = next(
        row for row in await repository.list_purposes("codebuddy", account_id)
        if row["purpose"] == "checkin"
    )
    assert purpose["verification_status"] == "verified"
    assert "credential.cookie" in purpose["capabilities"]


@pytest.mark.asyncio
async def test_rejected_workbuddy_import_preserves_existing_credential(onboarding_context) -> None:
    app, repository, vault, registry = onboarding_context
    account_id = await persist_codebuddy_account(
        repository, vault, label="existing", source="manual", access_token="chat-secret"
    )
    await persist_codebuddy_checkin(
        repository,
        vault,
        account_id=account_id,
        mode="bearer",
        access_token="old-checkin-secret",
        verified_at="2026-07-24T00:00:00+00:00",
    )
    await registry.rebuild()
    app.state.workbuddy_client_factory = lambda: _WorkBuddyProbe(CheckInOutcome.AUTH_FAILED)

    response = await _post(
        app,
        "/api/admin/auth/codebuddy/checkin",
        {"account_id": account_id, "access_token": "new-checkin-secret"},
    )

    assert response.status_code == 400
    assert "new-checkin-secret" not in response.text
    credential = await repository.get_credential("codebuddy", account_id, "checkin")
    assert credential is not None
    assert credential["credential_version"] == 1
    assert vault.decrypt(credential["encrypted_payload"])["access_token"] == "old-checkin-secret"


@pytest.mark.asyncio
async def test_codebuddy_reauthorization_keeps_account_and_checkin_purpose(onboarding_context) -> None:
    _app, repository, vault, _registry = onboarding_context
    account_id = await persist_codebuddy_account(
        repository, vault, label="primary", source="manual", access_token="old-chat"
    )
    await persist_codebuddy_checkin(
        repository,
        vault,
        account_id=account_id,
        mode="bearer",
        access_token="checkin-secret",
        verified_at="2026-07-24T00:00:00+00:00",
    )

    durable_id = await persist_codebuddy_account(
        repository,
        vault,
        label="primary",
        source="oauth",
        access_token="new-chat",
        refresh_token="refresh-secret",
        account_id=account_id,
    )

    assert durable_id == account_id
    credentials = await repository.list_credential_metadata("codebuddy")
    assert {(row["account_id"], row["purpose"]) for row in credentials} == {
        (account_id, "chat"),
        (account_id, "checkin"),
    }
    purposes = {row["purpose"]: row for row in await repository.list_purposes("codebuddy", account_id)}
    assert purposes["checkin"]["verification_status"] == "verified"
    assert purposes["checkin"]["enabled"] is True
    chat = await repository.get_credential("codebuddy", account_id, "chat")
    assert chat is not None
    assert vault.decrypt(chat["encrypted_payload"])["access_token"] == "new-chat"


@pytest.mark.asyncio
async def test_oauth_reauthorization_polls_into_the_existing_account(onboarding_context) -> None:
    app, repository, vault, registry = onboarding_context
    account_id = await persist_codebuddy_account(
        repository, vault, label="primary", source="manual", access_token="old-chat"
    )
    await persist_codebuddy_checkin(
        repository,
        vault,
        account_id=account_id,
        mode="bearer",
        access_token="checkin-secret",
        verified_at="2026-07-24T00:00:00+00:00",
    )
    await registry.rebuild()
    app.state.codebuddy_oauth = _OAuthSuccess()

    started = await _post(
        app,
        "/api/admin/auth/codebuddy/start",
        {"account_id": account_id, "label": "primary"},
    )
    assert started.status_code == 200
    assert started.json()["account_id"] == account_id
    assert "raw-auth-state" not in started.text

    completed = await _post(
        app,
        "/api/admin/auth/codebuddy/poll",
        {"flow_id": started.json()["flow_id"]},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "success"
    assert completed.json()["account"]["account_id"] == account_id
    assert "rotated-access-token" not in completed.text
    metadata = await repository.list_credential_metadata("codebuddy")
    assert {(row["account_id"], row["purpose"]) for row in metadata} == {
        (account_id, "chat"),
        (account_id, "checkin"),
    }
    chat = await repository.get_credential("codebuddy", account_id, "chat")
    assert chat is not None
    assert chat["credential_version"] == 2


@pytest.mark.asyncio
async def test_canonical_credential_rotation_endpoint_keeps_legacy_alias(onboarding_context) -> None:
    app, repository, vault, registry = onboarding_context
    account_id = await persist_qoder_chat(
        repository, vault, label="qoder", pat="old-pat"
    )
    await registry.rebuild()

    response = await _post(
        app,
        f"/api/admin/credentials/qoder/{account_id}/chat/rotate",
        {"pat": "new-pat", "credential_version": 1},
    )

    assert response.status_code == 200
    assert response.json()["credential_version"] == 2
    assert "new-pat" not in response.text
    legacy = await _request(
        app,
        "PATCH",
        f"/api/admin/credentials/qoder/{account_id}/chat",
        {"pat": "next-pat", "credential_version": 2},
    )
    assert legacy.status_code == 200
    assert legacy.json()["credential_version"] == 3


def test_flow_store_keeps_reauthorization_target_without_raw_state() -> None:
    store = FlowStore()
    flow = store.create(
        label="primary",
        auth_state="raw-state",
        auth_url="https://example.test/auth",
        account_id="codebuddy-abc",
    )

    assert flow.account_id == "codebuddy-abc"
    assert "raw-state" not in repr(flow)


async def _post(app: FastAPI, path: str, body: dict[str, str]) -> httpx.Response:
    return await _request(app, "POST", path, body)


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    body: dict[str, str],
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        return await client.request(
            method,
            path,
            headers={"Authorization": "Bearer admin-secret"},
            json=body,
        )
