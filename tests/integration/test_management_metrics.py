"""Management metrics refresh and operation contracts."""

from __future__ import annotations

import asyncio

import httpx
import pytest


class _FailingMetricsScheduler:
    async def refresh_once(self) -> dict[str, int]:
        raise RuntimeError("upstream-secret-must-not-leak")


class _BlockingMetricsScheduler:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def refresh_once(self) -> dict[str, int]:
        self.started.set()
        await asyncio.Event().wait()
        return {}


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


async def _poll_operation(client: httpx.AsyncClient, operation_id: str) -> dict:
    for _ in range(20):
        response = await client.get(f"/api/admin/metrics/refresh/{operation_id}", headers=_headers())
        if response.status_code == 200 and response.json()["status"] != "running":
            return response.json()
        await asyncio.sleep(0)
    raise AssertionError("metrics refresh operation did not finish")


@pytest.mark.asyncio
async def test_metrics_detail_and_refresh_operation_are_trackable(management_context) -> None:
    app, repository, _refreshes = management_context
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        detail = await client.get("/api/admin/metrics/accounts/qoder/qd-1", headers=_headers())
        started = await client.post("/api/admin/metrics/refresh", headers=_headers())
        assert started.status_code == 202
        result = await _poll_operation(client, started.json()["operation_id"])

    assert detail.status_code == 200
    assert detail.json()["provider"] == "qoder"
    assert detail.json()["account_id"] == "qd-1"
    assert detail.json()["snapshots"][0]["metric_kind"] == "quota"
    assert result["status"] == "succeeded"
    assert result["result"]["fresh"] == 2
    events = [event for event in await repository.list_audit_events() if event["action"] == "metrics.refresh"]
    assert events and events[0]["result"] == "succeeded"


@pytest.mark.asyncio
async def test_metric_refresh_failure_and_cancellation_use_stable_codes(management_context, caplog) -> None:
    _app, repository, _refreshes = management_context
    failed_id = await repository.create_metric_refresh_operation()
    await repository.run_metric_refresh_operation(failed_id, _FailingMetricsScheduler())

    cancelled_id = await repository.create_metric_refresh_operation()
    scheduler = _BlockingMetricsScheduler()
    task = asyncio.create_task(repository.run_metric_refresh_operation(cancelled_id, scheduler))
    await scheduler.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    failed = await repository.get_metric_refresh_operation(failed_id)
    cancelled = await repository.get_metric_refresh_operation(cancelled_id)
    assert failed["status"] == "failed"
    assert failed["error_code"] == "metrics_refresh_failed"
    assert "RuntimeError" not in str(failed)
    assert "upstream-secret" not in str(failed)
    assert cancelled["status"] == "cancelled"
    assert cancelled["error_code"] == "refresh_cancelled"
    outcomes = {
        event["resource_id"]: event["result"] for event in await repository.list_audit_events()
        if event["action"] == "metrics.refresh"
    }
    assert outcomes[failed_id] == "failed"
    assert outcomes[cancelled_id] == "cancelled"
    assert "upstream-secret-must-not-leak" not in caplog.text
