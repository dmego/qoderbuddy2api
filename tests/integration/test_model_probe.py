"""Account and model probes use one fixed secret-free request contract."""

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
from qb2api.admin import catalog_routes
from qb2api.admin.auth import AdminSessionStore
from qb2api.admin.router import router as admin_router
from qb2api.config import Settings


class _ProbeProvider:
    def __init__(self, *, delay: float = 0) -> None:
        self.delay = delay
        self.requests = []
        self.closed = False

    async def complete(self, request):
        self.requests.append(request)
        await asyncio.sleep(self.delay)
        return {"choices": [{"message": {"content": "secret-completion"}}]}

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
async def probe_context(tmp_path, monkeypatch):
    repository = AccountRepository(str(tmp_path / "probe.sqlite3"))
    await repository.connect()
    await repository.migrate()
    vault = CredentialVault(Fernet.generate_key().decode())
    await repository.upsert_account(
        provider="qoder", account_id="qd-1", label="main", source="manual", enabled=True
    )
    await repository.upsert_purpose(
        provider="qoder",
        account_id="qd-1",
        purpose="chat",
        enabled=True,
        status="active",
        verification_status="verified",
    )
    await repository.upsert_credential(
        provider="qoder",
        account_id="qd-1",
        purpose="chat",
        mode="pat",
        encrypted_payload=vault.encrypt({"pat": "pat-secret"}),
    )
    await repository.upsert_model(
        provider="qoder",
        model_id="Qwen3.7-Max",
        capabilities=["chat"],
    )
    registry = AccountRegistry(repository, vault)
    await registry.rebuild()
    resolver = CredentialResolver(repository, vault, registry)
    provider = _ProbeProvider()

    async def build_provider(state, provider_name, account_id):
        assert provider_name == "qoder"
        assert account_id == "qd-1"
        return provider

    monkeypatch.setattr(catalog_routes, "_build_probe_provider", build_provider, raising=False)
    app = FastAPI()
    app.include_router(admin_router)
    app.state.settings = Settings(admin_key="admin-secret")
    app.state.account_repo = repository
    app.state.account_registry = registry
    app.state.credential_resolver = resolver
    app.state.admin_sessions = AdminSessionStore()
    app.state.refresh_provider_pools = registry.rebuild
    try:
        yield app, repository, provider
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_model_probe_uses_fixed_minimal_request_and_discards_content(probe_context) -> None:
    app, repository, provider = probe_context
    async with _client(app) as client:
        response = await client.post(
            "/api/admin/models/qoder/Qwen3.7-Max/probe", headers=_headers()
        )

    assert response.status_code == 200
    assert response.json()["status"] == "succeeded"
    assert response.json()["account_id"] == "qd-1"
    assert "secret-completion" not in response.text
    request = provider.requests[0]
    assert request.model == "Qwen3.7-Max"
    assert request.max_tokens == 1
    assert request.temperature == 0
    assert [(message.role, message.content) for message in request.messages] == [
        ("system", "Health check."),
        ("user", "Reply OK."),
    ]
    assert provider.closed is True
    audit = await repository.list_audit_events()
    assert audit[0]["action"] == "model.probe"


@pytest.mark.asyncio
async def test_account_probe_rejects_custom_upstream_material(probe_context) -> None:
    app, _repository, provider = probe_context
    async with _client(app) as client:
        rejected = await client.post(
            "/api/admin/accounts/qoder/qd-1/probe",
            headers=_headers(),
            json={"url": "https://attacker.invalid", "authorization": "secret"},
        )
        accepted = await client.post(
            "/api/admin/accounts/qoder/qd-1/probe", headers=_headers()
        )

    assert rejected.status_code == 400
    assert rejected.json()["detail"] == "probe_body_not_allowed"
    assert accepted.status_code == 200
    assert accepted.json()["model_id"] == "Qwen3.7-Max"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_probe_timeout_uses_stable_code_and_never_exposes_error(probe_context, monkeypatch) -> None:
    app, repository, provider = probe_context
    provider.delay = 0.05
    monkeypatch.setattr(catalog_routes, "PROBE_TIMEOUT_SECONDS", 0.001, raising=False)

    async with _client(app) as client:
        response = await client.post(
            "/api/admin/models/qoder/Qwen3.7-Max/probe", headers=_headers()
        )

    assert response.status_code == 504
    assert response.json()["detail"] == "probe_timeout"
    assert "pat-secret" not in response.text
    audit = await repository.list_audit_events()
    assert audit[0]["result"] == "failed"
    assert audit[0]["action"] == "model.probe"


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}
