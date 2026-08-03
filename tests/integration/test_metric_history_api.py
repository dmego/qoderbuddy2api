"""Admin API metric history endpoint contracts."""

from __future__ import annotations

import httpx
import pytest


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


@pytest.mark.asyncio
async def test_metric_history_endpoint_requires_admin_and_returns_rows(management_context):
    app, repo, _refreshes = management_context
    await repo.upsert_account(
        provider="codebuddy",
        account_id="cb-1",
        label="cb-1",
        source="manual",
        enabled=True,
    )
    await repo.upsert_purpose(
        provider="codebuddy",
        account_id="cb-1",
        purpose="checkin",
        enabled=True,
        status="active",
        verification_status="verified",
    )
    await app.state.account_registry.rebuild()
    await repo.upsert_metric_history(
        provider="codebuddy",
        account_id="cb-1",
        metric_kind="points",
        value={"total_remaining": 100},
        observed_at="2026-08-03T00:00:00+00:00",
    )
    await repo.upsert_metric_history(
        provider="codebuddy",
        account_id="cb-1",
        metric_kind="points",
        value={"total_remaining": 90},
        observed_at="2026-08-03T00:15:00+00:00",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        anonymous = await client.get(
            "/api/admin/metrics/accounts/codebuddy/cb-1/history/points?limit=1"
        )
        assert anonymous.status_code == 401
        response = await client.get(
            "/api/admin/metrics/accounts/codebuddy/cb-1/history/points?limit=1",
            headers=_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["metric_kind"] == "points"
        assert [r["value"]["total_remaining"] for r in body["rows"]] == [90]
        assert body["limit"] == 1
