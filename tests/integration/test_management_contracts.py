"""Management query, refresh, metrics, validation, and audit contracts."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.accounts.vault import CredentialVault
from qb2api.admin.auth import AdminSessionStore
from qb2api.admin.router import router as admin_router
from qb2api.checkin.service import CheckinInProgressError
from qb2api.config import Settings


class _MetricsScheduler:
    async def refresh_once(self) -> dict[str, int]:
        await asyncio.sleep(0)
        return {"fresh": 2, "stale": 0, "unknown": 1, "unavailable": 0, "skipped": 0}

    def status_snapshot(self) -> dict[str, object]:
        return {"enabled": True, "running": False, "backoff_until": None}


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
            "daily_states": [
                {
                    "provider": "qoder",
                    "account_id": "qd-1",
                    "terminal_outcome": "CLAIMED",
                }
            ],
        }


@pytest.fixture
async def management_context(tmp_path):
    repository = AccountRepository(str(tmp_path / "management.sqlite3"))
    await repository.connect()
    await repository.migrate()
    vault = CredentialVault(Fernet.generate_key().decode())
    registry = AccountRegistry(repository, vault)
    resolver = CredentialResolver(repository, vault, registry)
    await _seed(repository, vault)
    await registry.rebuild()

    app = FastAPI()
    app.include_router(admin_router)
    app.state.settings = Settings(admin_key="admin-secret")
    app.state.account_repo = repository
    app.state.credential_vault = vault
    app.state.account_registry = registry
    app.state.credential_resolver = resolver
    app.state.admin_sessions = AdminSessionStore()
    app.state.metrics_scheduler = _MetricsScheduler()
    refreshes: list[str] = []

    async def refresh() -> None:
        refreshes.append("refresh")
        await registry.rebuild()

    app.state.refresh_provider_pools = refresh
    try:
        yield app, repository, refreshes
    finally:
        await repository.close()


@pytest.mark.asyncio
async def test_account_refresh_filters_pagination_and_mutation_audit(management_context) -> None:
    app, repository, refreshes = management_context
    async with _client(app) as client:
        page = await client.get(
            "/api/admin/accounts?provider=qoder&source=manual&status=active&purpose=chat&limit=1",
            headers=_headers(),
        )
        refreshed = await client.post(
            "/api/admin/accounts/qoder/qd-1/refresh", headers=_headers()
        )
        invalid = await client.get(
            "/api/admin/accounts?provider=unknown", headers=_headers()
        )

    assert page.status_code == 200
    assert [item["account_id"] for item in page.json()["accounts"]] == ["qd-1"]
    assert page.json()["limit"] == 1
    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "succeeded"
    assert refreshes == ["refresh"]
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "invalid_provider"
    audit = await repository.list_audit_events()
    assert audit[0]["action"] == "account.refresh"


@pytest.mark.asyncio
async def test_committed_mutation_audit_survives_derived_refresh_failure(
    management_context,
) -> None:
    app, repository, _refreshes = management_context

    async def fail_refresh() -> None:
        raise RuntimeError("derived refresh failed")

    app.state.refresh_provider_pools = fail_refresh
    async with _client(app) as client:
        response = await client.delete(
            "/api/admin/accounts/qoder/qd-2", headers=_headers()
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "provider_pool_refresh_failed"
    assert not any(
        item["account_id"] == "qd-2" for item in await repository.list_accounts("qoder")
    )
    audit = await repository.list_audit_events()
    outcomes = {(item["action"], item["result"]) for item in audit}
    assert ("account.delete", "succeeded") in outcomes
    assert ("provider_pool.refresh", "failed") in outcomes


@pytest.mark.asyncio
async def test_manual_checkin_start_is_trackable_and_audits_busy_failure(
    management_context,
) -> None:
    app, _repository, _refreshes = management_context
    async with _client(app) as client:
        app.state.checkin_service = _ManualCheckinService("success")
        succeeded = await client.post("/api/admin/checkin/run", headers=_headers())
        app.state.checkin_service = _ManualCheckinService("busy")
        failed = await client.post("/api/admin/checkin/run", headers=_headers())
        audit = await client.get(
            "/api/admin/audit?action=checkin.run",
            headers=_headers(),
        )

    assert succeeded.status_code == 202
    assert succeeded.json() == {
        "operation_id": "manual-run",
        "run_id": "manual-run",
        "status": "running",
    }
    assert failed.status_code == 409
    events = audit.json()["events"]
    assert sorted(item["result"] for item in events) == ["failed", "running"]
    failed_event = next(item for item in events if item["result"] == "failed")
    assert failed_event["error_code"] == "checkin_run_in_progress"


@pytest.mark.asyncio
async def test_checkin_status_uses_console_outcome_shape(management_context) -> None:
    app, _repository, _refreshes = management_context
    app.state.checkin_service = _StatusCheckinService()
    async with _client(app) as client:
        response = await client.get("/api/admin/checkin/status", headers=_headers())

    assert response.status_code == 200
    assert response.json()["daily_states"][0]["terminal_outcome"] == "claimed"


@pytest.mark.asyncio
async def test_metrics_detail_and_refresh_operation_are_trackable(management_context) -> None:
    app, repository, _refreshes = management_context
    async with _client(app) as client:
        detail = await client.get(
            "/api/admin/metrics/accounts/qoder/qd-1", headers=_headers()
        )
        started = await client.post("/api/admin/metrics/refresh", headers=_headers())
        assert started.status_code == 202
        operation_id = started.json()["operation_id"]
        result = await _poll_operation(client, operation_id)

    assert detail.status_code == 200
    assert detail.json()["provider"] == "qoder"
    assert detail.json()["account_id"] == "qd-1"
    assert detail.json()["snapshots"][0]["metric_kind"] == "quota"
    assert result["status"] == "succeeded"
    assert result["result"]["fresh"] == 2
    audit = await repository.list_audit_events()
    refresh_events = [event for event in audit if event["action"] == "metrics.refresh"]
    assert refresh_events and refresh_events[0]["result"] == "succeeded"


@pytest.mark.asyncio
async def test_metric_refresh_failure_and_cancellation_use_stable_codes(
    management_context,
    caplog,
) -> None:
    _app, repository, _refreshes = management_context
    failed_id = await repository.create_metric_refresh_operation()
    await repository.run_metric_refresh_operation(failed_id, _FailingMetricsScheduler())

    cancelled_id = await repository.create_metric_refresh_operation()
    scheduler = _BlockingMetricsScheduler()
    task = asyncio.create_task(
        repository.run_metric_refresh_operation(cancelled_id, scheduler)
    )
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
    audit = await repository.list_audit_events()
    outcomes = {
        event["resource_id"]: event["result"]
        for event in audit
        if event["action"] == "metrics.refresh"
    }
    assert outcomes[failed_id] == "failed"
    assert outcomes[cancelled_id] == "cancelled"
    assert "upstream-secret-must-not-leak" not in caplog.text


@pytest.mark.asyncio
async def test_usage_and_audit_filters_reject_invalid_ranges(management_context) -> None:
    app, _repository, _refreshes = management_context
    async with _client(app) as client:
        bad_range = await client.get(
            "/api/admin/usage/events?started_after=2026-07-25T00:00:00Z"
            "&started_before=2026-07-24T00:00:00Z",
            headers=_headers(),
        )
        bad_time = await client.get(
            "/api/admin/audit?started_after=not-a-time", headers=_headers()
        )
        bad_limit = await client.get("/api/admin/audit?limit=0", headers=_headers())

    assert bad_range.status_code == 400
    assert bad_range.json()["detail"] == "invalid_time_range"
    assert bad_time.status_code == 400
    assert bad_time.json()["detail"] == "invalid_started_after"
    assert bad_limit.status_code == 400
    assert bad_limit.json()["detail"] == "invalid_limit"


@pytest.mark.asyncio
async def test_equivalent_timezones_select_the_same_usage_and_audit_rows(
    management_context,
) -> None:
    app, repository, _refreshes = management_context
    await repository.add_request_event(
        {
            "event_id": "evt-timezone",
            "request_id": "req-timezone",
            "provider": "qoder",
            "account_id": "qd-1",
            "model_id": "model-timezone",
            "protocol": "openai",
            "status": "succeeded",
            "latency_ms": 20,
            "started_at": "2026-07-24T00:00:00+00:00",
        }
    )
    audit_id = await repository.add_audit_event(
        actor_type="admin",
        actor_id=None,
        action="account.refresh",
        resource_type="account",
        resource_id="qoder:qd-1",
        result="succeeded",
    )
    await repository.db.execute(
        "UPDATE audit_events SET created_at=? WHERE event_id=?",
        ("2026-07-24T00:00:00+00:00", audit_id),
    )
    await repository.db.commit()

    async with _client(app) as client:
        for started_after in (
            "2026-07-24T00:00:00Z",
            "2026-07-24T00:00:00+00:00",
            "2026-07-24T08:00:00+08:00",
        ):
            params = {
                "started_after": started_after,
                "started_before": "2026-07-24T00:01:00+00:00",
            }
            usage = await client.get(
                "/api/admin/usage/events", headers=_headers(), params=params
            )
            audit = await client.get(
                "/api/admin/audit", headers=_headers(), params=params
            )
            assert [item["event_id"] for item in usage.json()["events"]] == [
                "evt-timezone"
            ]
            assert [item["event_id"] for item in audit.json()["events"]] == [audit_id]


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
        await repository.add_request_event(
            {
                "event_id": f"evt-success-{index}",
                "request_id": f"req-success-{index}",
                "provider": "qoder",
                "account_id": "qd-1",
                "model_id": "Qwen3.7-Max",
                "protocol": "openai",
                "status": "succeeded",
                "latency_ms": latency,
                "started_at": f"2026-07-24T00:00:0{index}+00:00",
            }
        )
    await repository.add_request_event(
        {
            "event_id": "evt-failed",
            "request_id": "req-failed",
            "provider": "qoder",
            "account_id": "qd-1",
            "model_id": "Qwen3.7-Max",
            "protocol": "openai",
            "status": "failed",
            "latency_ms": None,
            "started_at": "2026-07-24T00:00:04+00:00",
        }
    )
    account_event = await repository.add_audit_event(
        actor_type="admin",
        actor_id=None,
        action="account.delete",
        resource_type="account",
        resource_id="qoder:qd-1",
        result="succeeded",
    )
    failed_account_event = await repository.add_audit_event(
        actor_type="admin",
        actor_id=None,
        action="account.update",
        resource_type="account",
        resource_id="qoder:qd-1",
        result="failed",
        metadata={"error_code": "provider_pool_refresh_failed"},
    )
    await repository.add_audit_event(
        actor_type="admin",
        actor_id=None,
        action="credential.rotate",
        resource_type="credential",
        resource_id="qoder:qd-1:chat",
        result="succeeded",
    )

    async with _client(app) as client:
        models = await client.get(
            "/api/admin/models?search=max", headers=_headers()
        )
        queried_models = await client.get(
            "/api/admin/models?query=flash", headers=_headers()
        )
        summary = await client.get(
            "/api/admin/usage/summary?status=succeeded", headers=_headers()
        )
        failed_summary = await client.get(
            "/api/admin/usage/summary?status=failed", headers=_headers()
        )
        events = await client.get(
            "/api/admin/usage/events?status=failed", headers=_headers()
        )
        timeseries = await client.get(
            "/api/admin/usage/timeseries?status=failed", headers=_headers()
        )
        exported = await client.get(
            "/api/admin/usage/export?status=failed", headers=_headers()
        )
        audit = await client.get(
            "/api/admin/audit?action=account.delete&query=qd-1", headers=_headers()
        )
        prefix_audit = await client.get(
            "/api/admin/audit?action_prefix=account.&result=failed",
            headers=_headers(),
        )
        category_audit = await client.get(
            "/api/admin/audit?category=credential", headers=_headers()
        )
        invalid_status = await client.get(
            "/api/admin/usage/summary?status=unknown", headers=_headers()
        )
        invalid_category = await client.get(
            "/api/admin/audit?category=unknown", headers=_headers()
        )

    assert [item["model_id"] for item in models.json()["models"]] == ["Qwen3.7-Max"]
    assert [item["model_id"] for item in queried_models.json()["models"]] == [
        "Qwen3.7-Flash"
    ]
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
    assert [item["event_id"] for item in prefix_audit.json()["events"]] == [
        failed_account_event
    ]
    assert prefix_audit.json()["events"][0]["error_code"] == (
        "provider_pool_refresh_failed"
    )
    assert category_audit.json()["events"][0]["action"] == "credential.rotate"
    assert invalid_status.status_code == 400
    assert invalid_status.json()["detail"] == "invalid_status"
    assert invalid_category.status_code == 400
    assert invalid_category.json()["detail"] == "invalid_category"


@pytest.mark.asyncio
async def test_model_mutation_rolls_back_when_audit_insert_fails(
    management_context,
) -> None:
    app, repository, _refreshes = management_context
    await repository.upsert_model(
        provider="qoder", model_id="atomic-model", enabled=True
    )
    await repository.db.execute(
        """CREATE TRIGGER reject_model_audit BEFORE INSERT ON audit_events
        WHEN NEW.action='model.update'
        BEGIN SELECT RAISE(ABORT, 'audit rejected'); END"""
    )
    await repository.db.commit()
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        response = await client.patch(
            "/api/admin/models/qoder/atomic-model",
            headers=_headers(),
            json={"enabled": False},
        )

    model = next(
        item for item in await repository.list_models("qoder")
        if item["model_id"] == "atomic-model"
    )
    assert response.status_code == 500
    assert model["enabled"] is True


async def _seed(repository: AccountRepository, vault: CredentialVault) -> None:
    for account_id in ("qd-1", "qd-2"):
        await repository.upsert_account(
            provider="qoder",
            account_id=account_id,
            label=account_id,
            source="manual",
            enabled=True,
        )
        await repository.upsert_purpose(
            provider="qoder",
            account_id=account_id,
            purpose="chat",
            enabled=True,
            status="active",
            verification_status="verified",
        )
        await repository.upsert_credential(
            provider="qoder",
            account_id=account_id,
            purpose="chat",
            mode="pat",
            encrypted_payload=vault.encrypt({"pat": f"secret-{account_id}"}),
        )
    await repository.upsert_metric_snapshot(
        provider="qoder",
        account_id="qd-1",
        metric_kind="quota",
        value={"remaining": 3},
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://test",
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


async def _poll_operation(client: httpx.AsyncClient, operation_id: str) -> dict:
    for _ in range(20):
        response = await client.get(
            f"/api/admin/metrics/refresh/{operation_id}", headers=_headers()
        )
        if response.status_code == 200 and response.json()["status"] != "running":
            return response.json()
        await asyncio.sleep(0)
    raise AssertionError("metrics refresh operation did not finish")
