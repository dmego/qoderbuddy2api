"""Shared integration-test application and persistence fixtures."""

from __future__ import annotations

import asyncio

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.accounts.vault import CredentialVault
from qb2api.admin.auth import AdminSessionStore
from qb2api.admin.router import router as admin_router
from qb2api.config import Settings


class MetricsScheduler:
    async def refresh_once(self) -> dict[str, int]:
        await asyncio.sleep(0)
        return {"fresh": 2, "stale": 0, "unknown": 1, "unavailable": 0, "skipped": 0}

    def status_snapshot(self) -> dict[str, object]:
        return {"enabled": True, "running": False, "backoff_until": None}


@pytest.fixture
async def management_context(tmp_path):
    repository = AccountRepository(str(tmp_path / "management.sqlite3"))
    await repository.connect()
    await repository.migrate()
    vault = CredentialVault(Fernet.generate_key().decode())
    registry = AccountRegistry(repository, vault)
    resolver = CredentialResolver(repository, vault, registry)
    await seed_management(repository, vault)
    await registry.rebuild()

    app = _admin_app(repository, vault, registry, resolver)
    app.state.metrics_scheduler = MetricsScheduler()
    refreshes: list[str] = []

    async def refresh() -> None:
        refreshes.append("refresh")
        await registry.rebuild()

    app.state.refresh_provider_pools = refresh
    try:
        yield app, repository, refreshes
    finally:
        await repository.close()


@pytest.fixture
async def admin_context(tmp_path):
    repository = AccountRepository(str(tmp_path / "accounts.sqlite3"))
    await repository.connect()
    await repository.migrate()
    vault = CredentialVault(Fernet.generate_key().decode())
    registry = AccountRegistry(repository, vault)
    await registry.rebuild()
    resolver = CredentialResolver(repository, vault, registry)
    app = _admin_app(repository, vault, registry, resolver)

    async def refresh() -> None:
        await registry.rebuild()

    app.state.refresh_provider_pools = refresh
    try:
        yield app, repository, vault, registry
    finally:
        await repository.close()


@pytest.fixture
async def checkin_context(tmp_path):
    repository = AccountRepository(str(tmp_path / "accounts.sqlite3"))
    await repository.connect()
    await repository.migrate()
    vault = CredentialVault(Fernet.generate_key().decode())
    try:
        yield repository, vault
    finally:
        await repository.close()


def _admin_app(
    repository: AccountRepository,
    vault: CredentialVault,
    registry: AccountRegistry,
    resolver: CredentialResolver,
) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_router)
    app.state.settings = Settings(admin_key="admin-secret")
    app.state.account_repo = repository
    app.state.credential_vault = vault
    app.state.account_registry = registry
    app.state.credential_resolver = resolver
    app.state.admin_sessions = AdminSessionStore()
    return app


async def seed_management(repository: AccountRepository, vault: CredentialVault) -> None:
    for account_id in ("qd-1", "qd-2"):
        await repository.upsert_account(
            provider="qoder", account_id=account_id, label=account_id,
            source="manual", enabled=True,
        )
        await repository.upsert_purpose(
            provider="qoder", account_id=account_id, purpose="chat", enabled=True,
            status="active", verification_status="verified",
        )
        await repository.upsert_credential(
            provider="qoder", account_id=account_id, purpose="chat", mode="pat",
            encrypted_payload=vault.encrypt({"pat": f"secret-{account_id}"}),
        )
    await repository.upsert_metric_snapshot(
        provider="qoder", account_id="qd-1", metric_kind="quota", value={"remaining": 3},
    )
