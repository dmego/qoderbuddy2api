"""Management observability validation and time-boundary contracts."""

from __future__ import annotations

import httpx
import pytest


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


@pytest.mark.asyncio
async def test_usage_and_audit_filters_reject_invalid_ranges(management_context) -> None:
    app, _repository, _refreshes = management_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        bad_range = await client.get(
            "/api/admin/usage/events?started_after=2026-07-25T00:00:00Z&started_before=2026-07-24T00:00:00Z",
            headers=_headers(),
        )
        bad_time = await client.get("/api/admin/audit?started_after=not-a-time", headers=_headers())
        bad_limit = await client.get("/api/admin/audit?limit=0", headers=_headers())

    assert bad_range.status_code == 400
    assert bad_range.json()["detail"] == "invalid_time_range"
    assert bad_time.status_code == 400
    assert bad_time.json()["detail"] == "invalid_started_after"
    assert bad_limit.status_code == 400
    assert bad_limit.json()["detail"] == "invalid_limit"


@pytest.mark.asyncio
async def test_equivalent_timezones_select_the_same_usage_and_audit_rows(management_context) -> None:
    app, repository, _refreshes = management_context
    await repository.add_request_event({
        "event_id": "evt-timezone", "request_id": "req-timezone", "provider": "qoder",
        "account_id": "qd-1", "model_id": "model-timezone", "protocol": "openai",
        "status": "succeeded", "latency_ms": 20, "started_at": "2026-07-24T00:00:00+00:00",
    })
    audit_id = await repository.add_audit_event(
        actor_type="admin", actor_id=None, action="account.refresh", resource_type="account",
        resource_id="qoder:qd-1", result="succeeded",
    )
    await repository.db.execute(
        "UPDATE audit_events SET created_at=? WHERE event_id=?", ("2026-07-24T00:00:00+00:00", audit_id)
    )
    await repository.db.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        for started_after in (
            "2026-07-24T00:00:00Z", "2026-07-24T00:00:00+00:00", "2026-07-24T08:00:00+08:00",
        ):
            params = {"started_after": started_after, "started_before": "2026-07-24T00:01:00+00:00"}
            usage = await client.get("/api/admin/usage/events", headers=_headers(), params=params)
            audit = await client.get("/api/admin/audit", headers=_headers(), params=params)
            assert [item["event_id"] for item in usage.json()["events"]] == ["evt-timezone"]
            assert [item["event_id"] for item in audit.json()["events"]] == [audit_id]
