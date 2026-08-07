"""Account credential rotation and revocation contracts."""

from __future__ import annotations

import httpx
import pytest

from qb2api.accounts.imports import persist_qoder_chat


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


@pytest.mark.asyncio
async def test_rotating_chat_credential_requires_reauth_and_removes_pool_slot(admin_context) -> None:
    app, repository, vault, registry = admin_context
    account_id = await persist_qoder_chat(repository, vault, label="main", pat="old-pat-secret")
    await app.state.refresh_provider_pools()
    assert [(slot.provider, slot.account_id) for slot in registry.snapshot("chat")] == [("qoder", account_id)]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        response = await client.patch(
            f"/api/admin/credentials/qoder/{account_id}/chat", headers=_headers(),
            json={"pat": "new-pat-secret", "credential_version": 1},
        )

    assert response.status_code == 200
    assert "old-pat-secret" not in response.text
    assert "new-pat-secret" not in response.text
    assert response.json() == {"status": "succeeded", "credential_version": 2, "verification_status": "unverified"}
    purpose = next(item for item in await repository.list_purposes("qoder", account_id) if item["purpose"] == "chat")
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
    account_id = await persist_qoder_chat(repository, vault, label="main", pat="pat-secret")
    await app.state.refresh_provider_pools()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        response = await client.delete(f"/api/admin/credentials/qoder/{account_id}/chat", headers=_headers())

    assert response.status_code == 200
    assert await repository.get_credential("qoder", account_id, "chat") is None
    purpose = next(item for item in await repository.list_purposes("qoder", account_id) if item["purpose"] == "chat")
    assert purpose["enabled"] is False
    assert purpose["status"] == "needs_reauth"
    assert purpose["verification_status"] == "unverified"
    assert registry.snapshot("chat") == []
