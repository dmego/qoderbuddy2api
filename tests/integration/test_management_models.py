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
    )
    await repository.upsert_model(
        provider="qoder",
        model_id="Qwen3.7-Flash",
        display_name="Qwen Flash",
        capabilities=["chat"],
    )
    for index, latency in enumerate((100, 200, 300), start=1):
        await repository.add_request_event({
            "event_id": f"evt-success-{index}", "request_id": f"req-success-{index}",
            "provider": "qoder", "account_id": "qd-1", "model_id": "Qwen3.7-Max",
            "protocol": "openai", "status": "succeeded", "latency_ms": latency,
            "started_at": f"2026-07-24T00:00:0{index}+00:00",
        })
    await repository.add_request_event({
        "event_id": "evt-failed", "request_id": "req-failed", "provider": "qoder", "account_id": "qd-1",
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
        models = await client.get("/api/admin/models?search=max", headers=_headers())
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

    assert [item["model_id"] for item in models.json()["models"]] == ["Qwen3.7-Max"]
    assert [item["model_id"] for item in queried_models.json()["models"]] == ["Qwen3.7-Flash"]
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
