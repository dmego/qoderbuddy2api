"""Control Plane persistence contracts for settings, service, and telemetry."""

from __future__ import annotations

import pytest

from qb2api.accounts.repository import AccountRepository


@pytest.fixture
async def repository(tmp_path):
    current = AccountRepository(str(tmp_path / "control.sqlite3"))
    await current.connect()
    await current.migrate()
    try:
        yield current
    finally:
        await current.close()


@pytest.mark.asyncio
async def test_runtime_settings_use_optimistic_versions(repository) -> None:
    version = await repository.upsert_runtime_setting(
        key="checkin.schedule.at",
        value="00:10",
        expected_version=0,
        apply_mode="scheduler_reschedule",
    )
    assert version == 1
    assert (await repository.get_runtime_setting("checkin.schedule.at"))["value"] == "00:10"

    with pytest.raises(ValueError, match="version conflict"):
        await repository.upsert_runtime_setting(
            key="checkin.schedule.at",
            value="01:20",
            expected_version=0,
        )


@pytest.mark.asyncio
async def test_service_runtime_and_audit_are_persisted(repository) -> None:
    await repository.save_service_runtime(
        "proxy-worker",
        {
            "desired_state": "RUNNING",
            "observed_state": "HEALTHY",
            "worker_pid": 123,
            "owner_instance_id": "owner-1",
        },
    )
    service = await repository.get_service_runtime("proxy-worker")
    assert service["worker_pid"] == 123
    assert service["observed_state"] == "HEALTHY"

    await repository.add_audit_event(
        actor_type="admin",
        actor_id="session",
        action="service.start",
        resource_type="service",
        resource_id="proxy-worker",
        result="succeeded",
    )
    assert (await repository.list_audit_events())[0]["action"] == "service.start"


@pytest.mark.asyncio
async def test_catalog_events_and_unknown_metric_status(repository) -> None:
    await repository.upsert_model(
        provider="codebuddy",
        model_id="claude-sonnet",
        capabilities=["chat", "tools"],
    )
    await repository.add_request_event(
        {
            "event_id": "event-1",
            "request_id": "request-1",
            "provider": "codebuddy",
            "model_id": "claude-sonnet",
            "protocol": "openai",
            "status": "succeeded",
            "stream_committed": True,
        }
    )
    await repository.upsert_metric_snapshot(
        provider="codebuddy",
        account_id="cb-1",
        metric_kind="points",
        value=None,
        status="unknown",
    )

    assert (await repository.list_models())[0]["capabilities"] == ["chat", "tools"]
    assert (await repository.list_request_events())[0]["stream_committed"] is True
    metric = (await repository.list_metric_snapshots())[0]
    assert metric["status"] == "unknown"
    assert metric["value"] is None
