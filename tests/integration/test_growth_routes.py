"""Growth center read-only API contracts."""

from __future__ import annotations

import httpx
import pytest

from qb2api.checkin import growth as growth_mod


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


@pytest.mark.asyncio
async def test_growth_overview_returns_tasks_and_profile(admin_context, monkeypatch) -> None:
    app, repository, vault, registry = admin_context
    account_id = "cb-growth-1"
    await repository.upsert_account(
        provider="codebuddy", account_id=account_id, label="growth",
        source="manual", enabled=True,
    )
    await repository.upsert_purpose(
        provider="codebuddy", account_id=account_id, purpose="chat", enabled=True,
        status="active", verification_status="not_required",
    )
    await repository.upsert_credential(
        provider="codebuddy", account_id=account_id, purpose="chat", mode="bearer",
        encrypted_payload=vault.encrypt({"access_token": "growth-token"}),
    )
    await registry.rebuild()

    class FakeGrowthClient:
        def __init__(self, *, base_url, timeout):
            pass

        async def fetch(self, token):
            assert token == "growth-token"
            return {
                "profile": {"level": 7, "completed": 7, "total": 13},
                "tasks": [{"task_code": "chat_5", "title": "和 AI 聊天 5 次",
                            "accept_status": "accepted", "progress_current": 5,
                            "progress_target": 5, "has_reward": True,
                            "reward_credit": 100}],
            }

        async def aclose(self):
            return None

    monkeypatch.setattr(growth_mod, "WorkBuddyGrowthClient", FakeGrowthClient)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.get(
            f"/api/admin/accounts/codebuddy/{account_id}/growth",
            headers=_headers(),
        )

    assert response.status_code == 200
    data = response.json()
    assert data["profile"]["level"] == 7
    assert data["tasks"][0]["task_code"] == "chat_5"
    assert data["tasks"][0]["progress_current"] == 5


@pytest.mark.asyncio
async def test_growth_overview_rejects_non_codebuddy_provider(admin_context) -> None:
    app, _repository, _vault, registry = admin_context
    registry.set_env_tokens(qoder_tokens=["env-pat"])
    await registry.rebuild()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.get(
            "/api/admin/accounts/qoder/qd-env-0/growth",
            headers=_headers(),
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "unsupported_provider"
