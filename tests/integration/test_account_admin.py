"""HTTP contracts for atomic, redacted account imports."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from qb2api.accounts.imports import persist_qoder_chat
from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.accounts.vault import CredentialVault
from qb2api.admin.auth import AdminSessionStore
from qb2api.admin.router import router as admin_router
from qb2api.checkin.models import CheckInOutcome, CheckInResult
from qb2api.config import Settings


class _QoderProbe:
    def __init__(self, result: CheckInResult) -> None:
        self.result = result

    async def status(self, **_values) -> CheckInResult:
        return self.result


@pytest.fixture
async def admin_context(tmp_path):
    repository = AccountRepository(str(tmp_path / "accounts.sqlite3"))
    await repository.connect()
    await repository.migrate()
    vault = CredentialVault(Fernet.generate_key().decode())
    registry = AccountRegistry(repository, vault)
    await registry.rebuild()
    resolver = CredentialResolver(repository, vault, registry)
    app = FastAPI()
    app.include_router(admin_router)
    app.state.settings = Settings(admin_key="admin-secret")
    app.state.account_repo = repository
    app.state.credential_vault = vault
    app.state.account_registry = registry
    app.state.credential_resolver = resolver
    app.state.admin_sessions = AdminSessionStore()

    async def refresh() -> None:
        await registry.rebuild()

    app.state.refresh_provider_pools = refresh
    yield app, repository, vault, registry
    await repository.close()


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


@pytest.mark.asyncio
async def test_qoder_import_rejects_unknown_probe_without_mutation(admin_context) -> None:
    app, repository, vault, registry = admin_context
    account_id = await persist_qoder_chat(
        repository,
        vault,
        label="main",
        pat="pat-secret",
    )
    await registry.rebuild()
    app.state.checkin_service = SimpleNamespace(
        qoder_client=_QoderProbe(
            CheckInResult(
                outcome=CheckInOutcome.FAILED,
                provider="qoder",
                raw_status="UNKNOWN",
            )
        )
    )

    response = await _post_qoder_checkin(app, account_id)

    assert response.status_code == 400
    purposes = await repository.list_purposes("qoder", account_id)
    checkin = next(item for item in purposes if item["purpose"] == "checkin")
    assert checkin["status"] == "needs_import"
    assert await repository.get_credential("qoder", account_id, "checkin") is None


@pytest.mark.asyncio
async def test_qoder_import_commits_verified_credential_and_redacts_response(
    admin_context,
) -> None:
    app, repository, vault, registry = admin_context
    account_id = await persist_qoder_chat(
        repository,
        vault,
        label="main",
        pat="pat-secret",
    )
    await registry.rebuild()
    app.state.checkin_service = SimpleNamespace(
        qoder_client=_QoderProbe(
            CheckInResult(
                outcome=CheckInOutcome.SKIPPED,
                provider="qoder",
                raw_status="CLAIMABLE",
            )
        )
    )

    response = await _post_qoder_checkin(app, account_id)

    assert response.status_code == 200
    assert "access-secret" not in response.text
    assert "refresh-secret" not in response.text
    purposes = await repository.list_purposes("qoder", account_id)
    checkin = next(item for item in purposes if item["purpose"] == "checkin")
    assert checkin["status"] == "active"
    assert checkin["verification_status"] == "verified"
    assert await repository.get_credential("qoder", account_id, "checkin") is not None


@pytest.mark.asyncio
async def test_codebuddy_manual_import_does_not_enable_unverified_checkin(
    admin_context,
) -> None:
    app, repository, _vault, _registry = admin_context
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post(
            "/api/admin/auth/codebuddy/manual",
            headers=_headers(),
            json={"label": "manual", "access_token": "bearer-secret"},
        )

    assert response.status_code == 200
    assert "bearer-secret" not in response.text
    account_id = response.json()["account"]["account_id"]
    purposes = await repository.list_purposes("codebuddy", account_id)
    checkin = next(item for item in purposes if item["purpose"] == "checkin")
    assert checkin["enabled"] is False
    assert checkin["verification_status"] == "unverified"
    assert await repository.get_credential("codebuddy", account_id, "checkin") is None


@pytest.mark.asyncio
async def test_rotating_chat_credential_requires_reauth_and_removes_pool_slot(
    admin_context,
) -> None:
    app, repository, vault, registry = admin_context
    account_id = await persist_qoder_chat(
        repository,
        vault,
        label="main",
        pat="old-pat-secret",
    )
    await app.state.refresh_provider_pools()
    assert [(slot.provider, slot.account_id) for slot in registry.snapshot("chat")] == [
        ("qoder", account_id)
    ]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.patch(
            f"/api/admin/credentials/qoder/{account_id}/chat",
            headers=_headers(),
            json={"pat": "new-pat-secret", "credential_version": 1},
        )

    assert response.status_code == 200
    assert "old-pat-secret" not in response.text
    assert "new-pat-secret" not in response.text
    assert response.json() == {
        "status": "succeeded",
        "credential_version": 2,
        "verification_status": "unverified",
    }
    purpose = next(
        item
        for item in await repository.list_purposes("qoder", account_id)
        if item["purpose"] == "chat"
    )
    assert purpose["enabled"] is True
    assert purpose["status"] == "needs_reauth"
    assert purpose["verification_status"] == "unverified"
    credential = await repository.get_credential("qoder", account_id, "chat")
    assert credential["fingerprint_hmac"] == vault.fingerprint("new-pat-secret")
    assert credential["fingerprint_hmac"] != "new-pat-secret"
    assert registry.snapshot("chat") == []


@pytest.mark.asyncio
async def test_revoking_chat_credential_marks_purpose_unverified(admin_context) -> None:
    app, repository, vault, registry = admin_context
    account_id = await persist_qoder_chat(
        repository,
        vault,
        label="main",
        pat="pat-secret",
    )
    await app.state.refresh_provider_pools()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.delete(
            f"/api/admin/credentials/qoder/{account_id}/chat",
            headers=_headers(),
        )

    assert response.status_code == 200
    assert await repository.get_credential("qoder", account_id, "chat") is None
    purpose = next(
        item
        for item in await repository.list_purposes("qoder", account_id)
        if item["purpose"] == "chat"
    )
    assert purpose["enabled"] is False
    assert purpose["status"] == "needs_reauth"
    assert purpose["verification_status"] == "unverified"
    assert registry.snapshot("chat") == []


@pytest.mark.asyncio
async def test_checkin_run_history_is_paginated_and_secret_safe(admin_context) -> None:
    app, repository, _vault, _registry = admin_context
    for run_id in ("run-earlier", "run-later"):
        await repository.create_checkin_run(
            run_id=run_id,
            local_date="2026-07-23",
            timezone="Asia/Shanghai",
            trigger="manual",
        )
        await repository.finish_checkin_run(run_id)
        await repository.upsert_checkin_attempt(
            run_id=run_id,
            provider="qoder",
            account_id="qd-main",
            outcome="CLAIMED",
            redacted_error="upstream-secret-must-not-appear",
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.get("/api/admin/checkin/runs?limit=1", headers=_headers())

    assert response.status_code == 200
    history = response.json()
    assert history["limit"] == 1
    assert len(history["runs"]) == 1
    assert history["runs"][0]["run_id"] == "run-later"
    assert history["runs"][0]["attempt_count"] == 1
    assert "upstream-secret-must-not-appear" not in response.text


async def _post_qoder_checkin(app: FastAPI, account_id: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        return await client.post(
            "/api/admin/auth/qoder/checkin",
            headers=_headers(),
            json={
                "account_id": account_id,
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
            },
        )
