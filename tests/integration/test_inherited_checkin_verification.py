"""Account-detail check-in verification reuses a CodeBuddy chat credential."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from qb2api.accounts.imports import persist_codebuddy_account
from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.accounts.vault import CredentialVault
from qb2api.admin.auth import AdminSessionStore
from qb2api.admin.router import router as admin_router
from qb2api.checkin.models import CheckInOutcome, CheckInResult
from qb2api.checkin.service import CheckinService
from qb2api.config import Settings


class _WorkBuddyProbe:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.closed = False

    async def checkin(self, **values: object) -> CheckInResult:
        self.calls.append(values)
        return CheckInResult(
            outcome=CheckInOutcome.ALREADY_CHECKED_IN,
            provider="codebuddy",
            account_id=str(values["account_id"]),
        )

    async def close(self) -> None:
        self.closed = True


class _NoopQoder:
    async def close(self) -> None:
        return None


@pytest.fixture
async def context(tmp_path):
    repository = AccountRepository(str(tmp_path / "accounts.sqlite3"))
    await repository.connect()
    await repository.migrate()
    vault = CredentialVault(Fernet.generate_key().decode())
    registry = AccountRegistry(repository, vault)
    resolver = CredentialResolver(repository, vault, registry)
    await registry.rebuild()
    settings = Settings(admin_key="admin-secret")
    probe = _WorkBuddyProbe()
    service = CheckinService(
        settings=settings,
        repo=repository,
        registry=registry,
        resolver=resolver,
        vault=vault,
        workbuddy=probe,
        qoder=_NoopQoder(),
    )
    application = FastAPI()
    application.include_router(admin_router)
    application.state.settings = settings
    application.state.account_repo = repository
    application.state.credential_vault = vault
    application.state.account_registry = registry
    application.state.credential_resolver = resolver
    application.state.admin_sessions = AdminSessionStore()
    application.state.checkin_service = service

    async def refresh() -> None:
        await registry.rebuild()

    application.state.refresh_provider_pools = refresh
    yield SimpleNamespace(
        application=application,
        repository=repository,
        vault=vault,
        registry=registry,
        probe=probe,
        service=service,
    )
    await service.close()
    await repository.close()


@pytest.mark.asyncio
async def test_account_verification_reuses_chat_credential_without_duplication(context) -> None:
    account_id = await persist_codebuddy_account(
        context.repository,
        context.vault,
        label="primary",
        source="oauth",
        access_token="chat-secret",
    )
    await context.registry.rebuild()
    transport = httpx.ASGITransport(app=context.application)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.post(
            f"/api/admin/accounts/codebuddy/{account_id}/verify-checkin",
            headers={"Authorization": "Bearer admin-secret"},
        )

    assert response.status_code == 200
    assert "chat-secret" not in response.text
    assert context.probe.calls[0]["auth_mode"] == "bearer"
    assert context.probe.calls[0]["access_token"] == "chat-secret"
    assert await context.repository.get_credential("codebuddy", account_id, "checkin") is None
    purposes = {
        item["purpose"]
        for item in await context.repository.list_purposes("codebuddy", account_id)
        if item["enabled"] and item["verification_status"] == "verified"
    }
    assert "checkin" in purposes
