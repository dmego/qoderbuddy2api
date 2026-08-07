"""Growth center read-only API contracts."""

from __future__ import annotations

import httpx
import pytest

from qb2api.checkin import growth as growth_mod
from qb2api.checkin import growth_automation as automation_mod


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


@pytest.mark.asyncio
async def test_growth_execute_runs_automation_for_manual_account(admin_context, monkeypatch) -> None:
    app, repository, vault, registry = admin_context
    account_id = "cb-growth-execute"
    await repository.upsert_account(
        provider="codebuddy", account_id=account_id, label=account_id,
        source="manual", enabled=True,
    )
    await repository.upsert_credential(
        provider="codebuddy", account_id=account_id, purpose="chat", mode="bearer",
        encrypted_payload=vault.encrypt({"access_token": "execute-token"}),
    )
    await registry.rebuild()
    calls: list[str] = []

    class FakeAutomation:
        def __init__(self, *, settings, repository=None):
            assert settings is app.state.settings
            assert repository is app.state.account_repo

        async def run(self, token, **_ctx):
            calls.append(token)
            return {"tasks": {"status": "skipped", "detail": "未启用"}}

        async def run_step(self, token, step, **_ctx):
            calls.append(f"{token}:{step}")
            return {"status": "completed", "detail": "ok"}

        async def close(self):
            return None

    monkeypatch.setattr(automation_mod, "GrowthAutomation", FakeAutomation)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.post(
            f"/api/admin/accounts/codebuddy/{account_id}/growth/execute",
            headers=_headers(), json={},
        )

    assert response.status_code == 200
    assert calls == ["execute-token"]
    assert response.json()["result"]["tasks"]["status"] == "skipped"


@pytest.mark.asyncio
async def test_growth_run_step_executes_single_step(admin_context, monkeypatch) -> None:
    app, repository, vault, registry = admin_context
    account_id = "cb-growth-step"
    await repository.upsert_account(
        provider="codebuddy", account_id=account_id, label=account_id,
        source="manual", enabled=True,
    )
    await repository.upsert_credential(
        provider="codebuddy", account_id=account_id, purpose="chat", mode="bearer",
        encrypted_payload=vault.encrypt({"access_token": "step-token"}),
    )
    await registry.rebuild()

    class FakeAutomation:
        def __init__(self, *, settings, repository=None):
            pass

        async def run_step(self, token, step, **_ctx):
            assert token == "step-token"
            assert step == "lottery"
            return {"status": "no_chances", "detail": "暂无抽奖次数"}

        async def close(self):
            return None

    monkeypatch.setattr(automation_mod, "GrowthAutomation", FakeAutomation)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.post(
            f"/api/admin/accounts/codebuddy/{account_id}/growth/run/lottery",
            headers=_headers(), json={},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["step"] == "lottery"
    assert body["result"]["status"] == "no_chances"


@pytest.mark.asyncio
async def test_growth_execute_rejects_env_account(admin_context) -> None:
    app, _repository, _vault, registry = admin_context
    registry.set_env_tokens(codebuddy_tokens=["env-token"])
    await registry.rebuild()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.post(
            "/api/admin/accounts/codebuddy/cb-env-0/growth/execute",
            headers=_headers(), json={},
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "env_account_read_only"


@pytest.mark.asyncio
async def test_growth_active_day_rerun_forces_conversation(admin_context, monkeypatch) -> None:
    app, repository, vault, registry = admin_context
    account_id = "cb-growth-rerun"
    await repository.upsert_account(
        provider="codebuddy", account_id=account_id, label=account_id,
        source="manual", enabled=True,
    )
    await repository.upsert_credential(
        provider="codebuddy", account_id=account_id, purpose="chat", mode="bearer",
        encrypted_payload=vault.encrypt({"access_token": "rerun-token"}),
    )
    await registry.rebuild()
    calls: list[str] = []

    class FakeAutomation:
        def __init__(self, *, settings, repository=None):
            assert repository is app.state.account_repo

        async def rerun_active_day(self, token, *, account_id, local_date, timezone):
            calls.append(token)
            return {"status": "succeeded"}

        async def close(self):
            return None

    monkeypatch.setattr(automation_mod, "GrowthAutomation", FakeAutomation)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.post(
            f"/api/admin/accounts/codebuddy/{account_id}/growth/active-day/rerun",
            headers=_headers(), json={},
        )

    assert response.status_code == 200
    assert calls == ["rerun-token"]
    body = response.json()
    assert body["step"] == "active_day"
    assert body["result"]["status"] == "succeeded"
