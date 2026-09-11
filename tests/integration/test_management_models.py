"""Management model, usage, and audit-query contracts."""

from __future__ import annotations

import httpx
import pytest


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


@pytest.mark.asyncio
async def test_model_usage_and_audit_query_contracts(management_context) -> None:
    app, repository, _refreshes = management_context
    await repository.upsert_model(
        provider="qoder",
        model_id="Qwen3.7-Max",
        display_name="Qwen Max Production",
        capabilities=["chat"],
        source="upstream",
    )
    await repository.upsert_model(
        provider="qoder",
        model_id="Qwen3.7-Flash",
        display_name="Qwen Flash",
        capabilities=["chat"],
        source="upstream",
    )
    for index, latency in enumerate((100, 200, 300), start=1):
        await repository.add_request_event({
            "event_id": f"evt-success-{index}", "request_id": f"req-success-{index}",
            "provider": "codebuddy", "account_id": "cb-1", "model_id": "Qwen3.7-Max",
            "protocol": "openai", "status": "succeeded", "latency_ms": latency,
            "started_at": f"2026-07-24T00:00:0{index}+00:00",
        })
    await repository.add_request_event({
        "event_id": "evt-failed", "request_id": "req-failed", "provider": "codebuddy", "account_id": "cb-1",
        "model_id": "Qwen3.7-Max", "protocol": "openai", "status": "failed", "latency_ms": None,
        "started_at": "2026-07-24T00:00:04+00:00",
    })
    account_event = await repository.add_audit_event(
        actor_type="admin", actor_id=None, action="account.delete", resource_type="account",
        resource_id="qoder:qd-1", result="succeeded",
    )
    failed_account_event = await repository.add_audit_event(
        actor_type="admin", actor_id=None, action="account.update", resource_type="account",
        resource_id="qoder:qd-1", result="failed", metadata={"error_code": "provider_pool_refresh_failed"},
    )
    await repository.add_audit_event(
        actor_type="admin", actor_id=None, action="credential.rotate", resource_type="credential",
        resource_id="qoder:qd-1:chat", result="succeeded",
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        models = await client.get("/api/admin/models?search=qwen", headers=_headers())
        queried_models = await client.get("/api/admin/models?query=flash", headers=_headers())
        summary = await client.get("/api/admin/usage/summary?status=succeeded", headers=_headers())
        failed_summary = await client.get("/api/admin/usage/summary?status=failed", headers=_headers())
        events = await client.get("/api/admin/usage/events?status=failed", headers=_headers())
        timeseries = await client.get("/api/admin/usage/timeseries?status=failed", headers=_headers())
        exported = await client.get("/api/admin/usage/export?status=failed", headers=_headers())
        audit = await client.get("/api/admin/audit?action=account.delete&query=qd-1", headers=_headers())
        prefix_audit = await client.get("/api/admin/audit?action_prefix=account.&result=failed", headers=_headers())
        category_audit = await client.get("/api/admin/audit?category=credential", headers=_headers())
        invalid_status = await client.get("/api/admin/usage/summary?status=unknown", headers=_headers())
        invalid_category = await client.get("/api/admin/audit?category=unknown", headers=_headers())

    assert [item["model_id"] for item in models.json()["models"]] == ["qwen3.7-flash", "qwen3.7-max"]
    queried_ids = [item["model_id"] for item in queried_models.json()["models"]]
    assert "qwen3.7-flash" in queried_ids and "qwen3.7-max" not in queried_ids
    assert models.json()["models"][1]["routes"][0]["provider"] == "qoder"
    assert summary.json()["summary"]["request_count"] == 3
    assert summary.json()["summary"]["latency_avg_ms"] == 200
    assert summary.json()["summary"]["latency_p95_ms"] == 300
    assert failed_summary.json()["summary"]["request_count"] == 1
    assert failed_summary.json()["summary"]["latency_avg_ms"] is None
    assert failed_summary.json()["summary"]["latency_p95_ms"] is None
    assert [item["event_id"] for item in events.json()["events"]] == ["evt-failed"]
    assert timeseries.json()["rollups"][0]["request_count"] == 1
    assert "evt-failed" in exported.text
    assert "evt-success" not in exported.text
    assert [item["event_id"] for item in audit.json()["events"]] == [account_event]
    assert [item["event_id"] for item in prefix_audit.json()["events"]] == [failed_account_event]
    assert prefix_audit.json()["events"][0]["error_code"] == "provider_pool_refresh_failed"
    assert category_audit.json()["events"][0]["action"] == "credential.rotate"
    assert invalid_status.status_code == 400
    assert invalid_status.json()["detail"] == "invalid_status"
    assert invalid_category.status_code == 400
    assert invalid_category.json()["detail"] == "invalid_category"


@pytest.mark.asyncio
async def test_model_mutation_rolls_back_when_audit_insert_fails(management_context) -> None:
    app, repository, _refreshes = management_context
    await repository.upsert_model(provider="qoder", model_id="atomic-model", enabled=True)
    await repository.db.execute(
        """CREATE TRIGGER reject_model_audit BEFORE INSERT ON audit_events
        WHEN NEW.action='model.update'
        BEGIN SELECT RAISE(ABORT, 'audit rejected'); END"""
    )
    await repository.db.commit()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="https://test"
    ) as client:
        response = await client.patch(
            "/api/admin/models/qoder/atomic-model", headers=_headers(), json={"enabled": False}
        )

    model = next(item for item in await repository.list_models("qoder") if item["model_id"] == "atomic-model")
    assert response.status_code == 500
    assert model["enabled"] is True


@pytest.mark.asyncio
async def test_route_patch_refreshes_runtime(management_context) -> None:
    """PATCH /models/{provider}/{model_id} persists per-route state and reloads the worker."""
    app, repository, refreshes = management_context
    await repository.upsert_model(provider="codebuddy", model_id="glm-5.2", enabled=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.patch(
            "/api/admin/models/codebuddy/glm-5.2", headers=_headers(), json={"enabled": False}
        )

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    rows = await repository.list_models("codebuddy")
    assert next(row for row in rows if row["model_id"] == "glm-5.2")["enabled"] is False
    assert refreshes  # worker runtime reloaded so the route filter takes effect


@pytest.mark.asyncio
async def test_model_listing_annotates_worker_quota_blocks(management_context, monkeypatch) -> None:
    """Unified listing attaches live per-account quota blocks to the matching route."""
    from qb2api.admin import catalog_support

    app, repository, _refreshes = management_context
    await repository.upsert_model(provider="codebuddy", model_id="glm-5.2", enabled=True)

    async def fake_index(_state):
        return {("codebuddy", "glm-5.2"): [
            {"account_id": "cb-1", "blocked_until": "2026-09-11T09:53:33Z"},
        ]}

    monkeypatch.setattr(catalog_support, "quota_block_index", fake_index)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.get("/api/admin/models?search=glm-5.2", headers=_headers())

    model = response.json()["models"][0]
    route = next(route for route in model["routes"] if route["provider"] == "codebuddy")
    assert route["quota_blocked"] == [{"account_id": "cb-1", "blocked_until": "2026-09-11T09:53:33Z"}]


@pytest.mark.asyncio
async def test_routing_exposes_accounts_and_block_state(management_context) -> None:
    """The routing console needs each provider's accounts to offer per-account blocks."""
    app, repository, _refreshes = management_context
    await _seed_chat_account(repository, provider="codebuddy", account_id="cb-1")
    await _seed_chat_account(repository, provider="codebuddy", account_id="cb-2")
    await app.state.account_registry.rebuild()
    await repository.set_account_model_block(
        provider="codebuddy", account_id="cb-1", model_id="glm-5.2",
        reason="管理员手动停用", source="manual",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.get("/api/admin/routing", headers=_headers())

    assert response.status_code == 200
    model = next(
        item for item in response.json()["models"] if item["model_id"] == "glm-5.2"
    )
    route = next(route for route in model["routes"] if route["provider"] == "codebuddy")
    accounts = {account["account_id"]: account for account in route["accounts"]}
    assert set(accounts) == {"cb-1", "cb-2"}
    assert accounts["cb-1"]["blocked"] is True
    assert accounts["cb-1"]["block"]["reason"] == "管理员手动停用"
    assert accounts["cb-1"]["block"]["source"] == "manual"
    assert accounts["cb-2"]["blocked"] is False
    assert accounts["cb-2"]["block"] is None


@pytest.mark.asyncio
async def test_routing_account_block_endpoint_blocks_and_unblocks(management_context) -> None:
    app, repository, refreshes = management_context

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        blocked = await client.put(
            "/api/admin/routing/glm-5.2/accounts/codebuddy/cb-1",
            headers=_headers(),
            json={"blocked": True, "reason": "配额用尽"},
        )
        assert blocked.status_code == 200
        assert blocked.json() == {
            "status": "ok", "blocked": True, "model_id": "glm-5.2",
            "provider": "codebuddy", "account_id": "cb-1",
        }
        rows = await repository.list_account_model_blocks("glm-5.2")
        assert rows[0]["reason"] == "配额用尽"
        assert rows[0]["source"] == "manual"
        assert refreshes  # the worker must be told about the change

        unblocked = await client.put(
            "/api/admin/routing/glm-5.2/accounts/codebuddy/cb-1",
            headers=_headers(),
            json={"blocked": False},
        )
        assert unblocked.status_code == 200
        assert await repository.list_account_model_blocks("glm-5.2") == []


@pytest.mark.asyncio
async def test_routing_account_block_requires_a_boolean(management_context) -> None:
    app, _repository, _refreshes = management_context
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.put(
            "/api/admin/routing/glm-5.2/accounts/codebuddy/cb-1",
            headers=_headers(),
            json={"blocked": "yes"},
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "blocked_boolean_required"


@pytest.mark.asyncio
async def test_routing_account_block_defaults_the_reason(management_context) -> None:
    app, repository, _refreshes = management_context
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        await client.put(
            "/api/admin/routing/glm-5.2/accounts/codebuddy/cb-2",
            headers=_headers(),
            json={"blocked": True},
        )
    rows = await repository.list_account_model_blocks("glm-5.2")
    assert rows[0]["reason"] == "管理员手动停用"


@pytest.mark.asyncio
async def test_routing_account_block_is_scoped_to_one_model(management_context) -> None:
    """Blocking an account for one model must not touch another model."""
    app, repository, _refreshes = management_context
    await _seed_chat_account(repository, provider="codebuddy", account_id="cb-1")
    await app.state.account_registry.rebuild()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        await client.put(
            "/api/admin/routing/glm-5.2/accounts/codebuddy/cb-1",
            headers=_headers(),
            json={"blocked": True},
        )
        response = await client.get("/api/admin/routing", headers=_headers())

    models = {item["model_id"]: item for item in response.json()["models"]}
    blocked_route = next(r for r in models["glm-5.2"]["routes"] if r["provider"] == "codebuddy")
    other_route = next(r for r in models["glm-5.1"]["routes"] if r["provider"] == "codebuddy")
    assert next(a for a in blocked_route["accounts"] if a["account_id"] == "cb-1")["blocked"] is True
    assert next(a for a in other_route["accounts"] if a["account_id"] == "cb-1")["blocked"] is False


async def _seed_chat_account(repository, *, provider: str, account_id: str) -> None:
    """Add one chat-capable account so the routing grid has something to list."""
    from cryptography.fernet import Fernet

    from qb2api.accounts.vault import CredentialVault

    vault = CredentialVault(Fernet.generate_key().decode())
    await repository.upsert_account(
        provider=provider, account_id=account_id, label=account_id,
        source="manual", enabled=True,
    )
    await repository.upsert_purpose(
        provider=provider, account_id=account_id, purpose="chat", enabled=True,
        status="active", verification_status="not_required",
    )
    await repository.upsert_credential(
        provider=provider, account_id=account_id, purpose="chat", mode="bearer",
        encrypted_payload=vault.encrypt({"access_token": f"token-{account_id}"}),
    )


@pytest.mark.asyncio
async def test_routing_merges_live_worker_blocks_without_failing(
    management_context, monkeypatch
) -> None:
    """Regression: live Worker blocks are a flat list, not the (provider, model) index.

    Iterating the index shape crashed the whole listing with a 500, and the
    stored-only path never exercised it.
    """
    from qb2api.admin import routing_routes

    app, repository, _refreshes = management_context
    await _seed_chat_account(repository, provider="codebuddy", account_id="cb-1")
    await app.state.account_registry.rebuild()

    async def fake_blocks(_state):
        return [{
            "provider": "codebuddy",
            "account_id": "cb-1",
            "model_id": "glm-5.2",
            "blocked_until": "2026-09-11T10:00:47Z",
            "reason": "您的使用量已超出频率限制",
            "source": "auto",
        }]

    monkeypatch.setattr(routing_routes, "quota_blocks", fake_blocks)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.get("/api/admin/routing", headers=_headers())

    assert response.status_code == 200
    model = next(
        item for item in response.json()["models"] if item["model_id"] == "glm-5.2"
    )
    route = next(route for route in model["routes"] if route["provider"] == "codebuddy")
    account = next(a for a in route["accounts"] if a["account_id"] == "cb-1")
    assert account["blocked"] is True
    assert account["block"]["source"] == "auto"
    assert account["block"]["reason"] == "您的使用量已超出频率限制"
    assert account["block"]["blocked_until"] == "2026-09-11T10:00:47Z"


@pytest.mark.asyncio
async def test_routing_prefers_the_stored_block_over_the_live_one(
    management_context, monkeypatch
) -> None:
    """A manual block is authoritative until the Worker picks up the new snapshot."""
    from qb2api.admin import routing_routes

    app, repository, _refreshes = management_context
    await _seed_chat_account(repository, provider="codebuddy", account_id="cb-1")
    await app.state.account_registry.rebuild()
    await repository.set_account_model_block(
        provider="codebuddy", account_id="cb-1", model_id="glm-5.2",
        reason="管理员手动停用", source="manual",
    )

    async def stale_live_blocks(_state):
        return [{
            "provider": "codebuddy", "account_id": "cb-1", "model_id": "glm-5.2",
            "blocked_until": "2026-09-11T10:00:47Z", "reason": "旧原因", "source": "auto",
        }]

    monkeypatch.setattr(routing_routes, "quota_blocks", stale_live_blocks)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        response = await client.get("/api/admin/routing", headers=_headers())

    model = next(
        item for item in response.json()["models"] if item["model_id"] == "glm-5.2"
    )
    route = next(route for route in model["routes"] if route["provider"] == "codebuddy")
    account = next(a for a in route["accounts"] if a["account_id"] == "cb-1")
    assert account["block"]["source"] == "manual"
    assert account["block"]["reason"] == "管理员手动停用"
