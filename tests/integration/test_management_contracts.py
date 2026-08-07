"""Management account and manual-check-in HTTP contracts."""

from __future__ import annotations

import httpx
import pytest

from qb2api.checkin.service import CheckinInProgressError


class _ManualCheckinService:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    async def start_batch(self, **_kwargs) -> str:
        if self.mode == "busy":
            raise CheckinInProgressError("busy")
        return "manual-run"


class _StatusCheckinService:
    async def status_snapshot(self, **_kwargs):
        return {
            "enabled": True,
            "running": False,
            "local_date": "2026-07-24",
            "timezone": "Asia/Shanghai",
            "checkin_at": "00:10",
            "eligible_accounts": [],
            "daily_states": [{
                "provider": "qoder", "account_id": "qd-1", "terminal_outcome": "CLAIMED",
            }],
        }


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


@pytest.mark.asyncio
async def test_account_refresh_filters_pagination_and_mutation_audit(management_context) -> None:
    app, repository, refreshes = management_context
    async with _client(app) as client:
        page = await client.get(
            "/api/admin/accounts?provider=qoder&source=manual&status=active&purpose=chat&limit=1",
            headers=_headers(),
        )
        refreshed = await client.post("/api/admin/accounts/qoder/qd-1/refresh", headers=_headers())
        invalid = await client.get("/api/admin/accounts?provider=unknown", headers=_headers())

    assert page.status_code == 200
    assert [item["account_id"] for item in page.json()["accounts"]] == ["qd-1"]
    assert page.json()["limit"] == 1
    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "succeeded"
    assert refreshes == ["refresh"]
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "invalid_provider"
    assert (await repository.list_audit_events())[0]["action"] == "account.refresh"


@pytest.mark.asyncio
async def test_committed_mutation_audit_survives_derived_refresh_failure(management_context) -> None:
    app, repository, _refreshes = management_context

    async def fail_refresh() -> None:
        raise RuntimeError("derived refresh failed")

    app.state.refresh_provider_pools = fail_refresh
    async with _client(app) as client:
        response = await client.delete("/api/admin/accounts/qoder/qd-2", headers=_headers())

    assert response.status_code == 503
    assert response.json()["detail"] == "provider_pool_refresh_failed"
    assert not any(item["account_id"] == "qd-2" for item in await repository.list_accounts("qoder"))
    outcomes = {(item["action"], item["result"]) for item in await repository.list_audit_events()}
    assert ("account.delete", "succeeded") in outcomes
    assert ("provider_pool.refresh", "failed") in outcomes


@pytest.mark.asyncio
async def test_manual_checkin_start_is_trackable_and_audits_busy_failure(management_context) -> None:
    app, _repository, _refreshes = management_context
    async with _client(app) as client:
        app.state.checkin_service = _ManualCheckinService("success")
        succeeded = await client.post("/api/admin/checkin/run", headers=_headers())
        app.state.checkin_service = _ManualCheckinService("busy")
        failed = await client.post("/api/admin/checkin/run", headers=_headers())
        audit = await client.get("/api/admin/audit?action=checkin.run", headers=_headers())

    assert succeeded.status_code == 202
    assert succeeded.json() == {"operation_id": "manual-run", "run_id": "manual-run", "status": "running"}
    assert failed.status_code == 409
    events = audit.json()["events"]
    assert sorted(item["result"] for item in events) == ["failed", "running"]
    assert next(item for item in events if item["result"] == "failed")["error_code"] == "checkin_run_in_progress"


@pytest.mark.asyncio
async def test_checkin_status_uses_console_outcome_shape(management_context) -> None:
    app, _repository, _refreshes = management_context
    app.state.checkin_service = _StatusCheckinService()
    async with _client(app) as client:
        response = await client.get("/api/admin/checkin/status", headers=_headers())

    assert response.status_code == 200
    assert response.json()["daily_states"][0]["terminal_outcome"] == "claimed"
