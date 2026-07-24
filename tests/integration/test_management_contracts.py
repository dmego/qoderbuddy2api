"""Management query, refresh, metrics, validation, and audit contracts."""

from __future__ import annotations

import asyncio

import httpx
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


class _MetricsScheduler:
    async def refresh_once(self) -> dict[str, int]:
        await asyncio.sleep(0)
        return {"fresh": 2, "stale": 0, "unknown": 1, "unavailable": 0, "skipped": 0}


@pytest.fixture
async def management_context(tmp_path):
    repository = AccountRepository(str(tmp_path / "management.sqlite3"))
    await repository.connect()
    await repository.migrate()
    vault = CredentialVault(Fernet.generate_key().decode())
    registry = AccountRegistry(repository, vault)
    resolver = CredentialResolver(repository, vault, registry)
    await _seed(repository, vault)
    await registry.rebuild()

    app = FastAPI()
    app.include_router(admin_router)
    app.state.settings = Settings(admin_key="admin-secret")
    app.state.account_repo = repository
    app.state.credential_vault = vault
    app.state.account_registry = registry
    app.state.credential_resolver = resolver
    app.state.admin_sessions = AdminSessionStore()
    app.state.metrics_scheduler = _MetricsScheduler()
    refreshes: list[str] = []

    async def refresh() -> None:
        refreshes.append("refresh")
        await registry.rebuild()

    app.state.refresh_provider_pools = refresh
    try:
        yield app, repository, refreshes
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_account_refresh_filters_pagination_and_mutation_audit(management_context) -> None:
    app, repository, refreshes = management_context
    async with _client(app) as client:
        page = await client.get(
            "/api/admin/accounts?provider=qoder&source=manual&status=active&purpose=chat&limit=1",
            headers=_headers(),
        )
        refreshed = await client.post(
            "/api/admin/accounts/qoder/qd-1/refresh", headers=_headers()
        )
        invalid = await client.get(
            "/api/admin/accounts?provider=unknown", headers=_headers()
        )

    assert page.status_code == 200
    assert [item["account_id"] for item in page.json()["accounts"]] == ["qd-1"]
    assert page.json()["limit"] == 1
    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "succeeded"
    assert refreshes == ["refresh"]
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "invalid_provider"
    audit = await repository.list_audit_events()
    assert audit[0]["action"] == "account.refresh"


@pytest.mark.asyncio
async def test_metrics_detail_and_refresh_operation_are_trackable(management_context) -> None:
    app, repository, _refreshes = management_context
    async with _client(app) as client:
        detail = await client.get(
            "/api/admin/metrics/accounts/qoder/qd-1", headers=_headers()
        )
        started = await client.post("/api/admin/metrics/refresh", headers=_headers())
        assert started.status_code == 202
        operation_id = started.json()["operation_id"]
        result = await _poll_operation(client, operation_id)

    assert detail.status_code == 200
    assert detail.json()["provider"] == "qoder"
    assert detail.json()["account_id"] == "qd-1"
    assert detail.json()["snapshots"][0]["metric_kind"] == "quota"
    assert result["status"] == "succeeded"
    assert result["result"]["fresh"] == 2
    audit = await repository.list_audit_events()
    refresh_events = [event for event in audit if event["action"] == "metrics.refresh"]
    assert refresh_events and refresh_events[0]["result"] == "succeeded"


@pytest.mark.asyncio
async def test_usage_and_audit_filters_reject_invalid_ranges(management_context) -> None:
    app, _repository, _refreshes = management_context
    async with _client(app) as client:
        bad_range = await client.get(
            "/api/admin/usage/events?started_after=2026-07-25T00:00:00Z"
            "&started_before=2026-07-24T00:00:00Z",
            headers=_headers(),
        )
        bad_time = await client.get(
            "/api/admin/audit?started_after=not-a-time", headers=_headers()
        )
        bad_limit = await client.get("/api/admin/audit?limit=0", headers=_headers())

    assert bad_range.status_code == 400
    assert bad_range.json()["detail"] == "invalid_time_range"
    assert bad_time.status_code == 400
    assert bad_time.json()["detail"] == "invalid_started_after"
    assert bad_limit.status_code == 400
    assert bad_limit.json()["detail"] == "invalid_limit"


async def _seed(repository: AccountRepository, vault: CredentialVault) -> None:
    for account_id in ("qd-1", "qd-2"):
        await repository.upsert_account(
            provider="qoder",
            account_id=account_id,
            label=account_id,
            source="manual",
            enabled=True,
        )
        await repository.upsert_purpose(
            provider="qoder",
            account_id=account_id,
            purpose="chat",
            enabled=True,
            status="active",
            verification_status="verified",
        )
        await repository.upsert_credential(
            provider="qoder",
            account_id=account_id,
            purpose="chat",
            mode="pat",
            encrypted_payload=vault.encrypt({"pat": f"secret-{account_id}"}),
        )
    await repository.upsert_metric_snapshot(
        provider="qoder",
        account_id="qd-1",
        metric_kind="quota",
        value={"remaining": 3},
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


async def _poll_operation(client: httpx.AsyncClient, operation_id: str) -> dict:
    for _ in range(20):
        response = await client.get(
            f"/api/admin/metrics/refresh/{operation_id}", headers=_headers()
        )
        if response.status_code == 200 and response.json()["status"] != "running":
            return response.json()
        await asyncio.sleep(0)
    raise AssertionError("metrics refresh operation did not finish")
