"""Admin route tests for POST /api/admin/models/sync/{provider}."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from qb2api.accounts.qoder_model_sync import SyncReport
from qb2api.admin import catalog_routes
from qb2api.admin.auth import AdminSessionStore
from qb2api.admin.router import router as admin_router
from qb2api.config import Settings
from qb2api.providers.qoder_auth import QoderError


@pytest.fixture
async def sync_context():
    repository = Mock()
    repository.add_audit_event = AsyncMock()
    app = FastAPI()
    app.include_router(admin_router)
    app.state.settings = Settings(admin_key="admin-secret")
    app.state.admin_sessions = AdminSessionStore()
    app.state.account_repo = repository
    app.state.account_registry = Mock()
    app.state.credential_resolver = Mock()
    yield app, repository


@pytest.mark.asyncio
async def test_sync_qoder_route_success(sync_context, monkeypatch) -> None:
    app, repository = sync_context
    report = SyncReport(
        added=2,
        updated=1,
        disabled=0,
        models=[{"model_id": "Qwen3.8-Max", "enabled": True}],
    )
    monkeypatch.setattr(catalog_routes, "sync_qoder_models", AsyncMock(return_value=report))

    async with _client(app) as client:
        response = await client.post("/api/admin/models/sync/qoder", headers=_headers())

    assert response.status_code == 200
    assert response.json() == {
        "status": "succeeded",
        "added": 2,
        "updated": 1,
        "disabled": 0,
        "models": [{"model_id": "Qwen3.8-Max", "enabled": True}],
    }
    repository.add_audit_event.assert_awaited_once()
    audit = repository.add_audit_event.await_args.kwargs
    assert audit["action"] == "model.sync"
    assert audit["resource_type"] == "qoder"
    assert audit["resource_id"] == "catalog"
    assert audit["result"] == "succeeded"
    assert audit["metadata"] == {"added": 2, "updated": 1, "disabled": 0}


@pytest.mark.asyncio
async def test_sync_qoder_route_rejects_other_provider(sync_context) -> None:
    app, repository = sync_context

    async with _client(app) as client:
        response = await client.post("/api/admin/models/sync/codebuddy", headers=_headers())

    assert response.status_code == 400
    assert response.json()["detail"] == "unsupported_provider"
    repository.add_audit_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_qoder_route_forwards_credential_error(sync_context, monkeypatch) -> None:
    app, repository = sync_context

    async def fail_sync(*args, **kwargs):
        raise QoderError("qoder rejected", status_code=401)

    monkeypatch.setattr(catalog_routes, "sync_qoder_models", fail_sync)

    async with _client(app) as client:
        response = await client.post("/api/admin/models/sync/qoder", headers=_headers())

    assert response.status_code == 401
    assert response.json()["detail"] == "sync_failed"
    repository.add_audit_event.assert_awaited_once()
    audit = repository.add_audit_event.await_args.kwargs
    assert audit["action"] == "model.sync"
    assert audit["result"] == "failed"
    assert audit["metadata"] == {"error_code": 401}


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}
