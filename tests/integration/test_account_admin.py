"""HTTP contracts for atomic, redacted account imports."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from qb2api.accounts.imports import persist_qoder_chat
from qb2api.checkin.models import CheckInOutcome, CheckInResult


class _QoderProbe:
    def __init__(self, result: CheckInResult) -> None:
        self.result = result

    async def status(self, **_values) -> CheckInResult:
        return self.result


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


async def _post_qoder_checkin(app, account_id: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        return await client.post(
            "/api/admin/auth/qoder/checkin",
            headers=_headers(),
            json={"account_id": account_id, "access_token": "access-secret", "refresh_token": "refresh-secret"},
        )


@pytest.mark.asyncio
async def test_qoder_import_rejects_unknown_probe_without_mutation(admin_context) -> None:
    app, repository, vault, registry = admin_context
    account_id = await persist_qoder_chat(repository, vault, label="main", pat="pat-secret")
    await registry.rebuild()
    app.state.checkin_service = SimpleNamespace(qoder_client=_QoderProbe(CheckInResult(
        outcome=CheckInOutcome.FAILED, provider="qoder", raw_status="UNKNOWN",
    )))

    response = await _post_qoder_checkin(app, account_id)

    assert response.status_code == 400
    purposes = await repository.list_purposes("qoder", account_id)
    checkin = next(item for item in purposes if item["purpose"] == "checkin")
    assert checkin["status"] == "needs_import"
    assert await repository.get_credential("qoder", account_id, "checkin") is None


@pytest.mark.asyncio
async def test_qoder_import_commits_verified_credential_and_redacts_response(admin_context) -> None:
    app, repository, vault, registry = admin_context
    account_id = await persist_qoder_chat(repository, vault, label="main", pat="pat-secret")
    await registry.rebuild()
    app.state.checkin_service = SimpleNamespace(qoder_client=_QoderProbe(CheckInResult(
        outcome=CheckInOutcome.SKIPPED, provider="qoder", raw_status="CLAIMABLE",
    )))

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
async def test_codebuddy_manual_import_does_not_enable_unverified_checkin(admin_context) -> None:
    app, repository, _vault, _registry = admin_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        response = await client.post(
            "/api/admin/auth/codebuddy/manual", headers=_headers(),
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
    actions = {
        item["action"] for item in await repository.list_audit_events()
        if item["resource_id"] and account_id in item["resource_id"]
    }
    assert {"account.import", "credential.import"} <= actions
