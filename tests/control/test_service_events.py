"""Persistent, secret-safe service lifecycle event contracts."""

from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from qb2api.config import Settings
from qb2api.control.app import create_control_app
from qb2api.control.supervisor import ServiceSupervisor


class _Process:
    pid = 5812
    start_time = 1.0
    process_group_id = 5812

    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode


def test_service_events_are_persisted_secret_safe_and_cursor_paginated(tmp_path) -> None:
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
        worker_autostart=False,
    )
    headers = {"Authorization": "Bearer admin-secret"}
    app = create_control_app(lambda: settings, supervisor_factory=_supervisor)

    with TestClient(app) as client:
        assert client.post("/api/admin/service/start", headers=headers).status_code == 200
        assert client.post("/api/admin/service/reload", headers=headers).status_code == 200

        first = client.get("/api/admin/service/events?limit=1", headers=headers)
        assert first.status_code == 200
        assert len(first.json()["events"]) == 1
        assert first.json()["next_cursor"] is not None
        assert "admin-secret" not in first.text
        assert "worker_internal_token" not in first.text

        second = client.get(
            f"/api/admin/service/events?limit=10&cursor={first.json()['next_cursor']}",
            headers=headers,
        )
        assert second.status_code == 200
        assert second.json()["events"]
        first_cursors = {event["cursor"] for event in first.json()["events"]}
        second_cursors = {event["cursor"] for event in second.json()["events"]}
        assert first_cursors.isdisjoint(second_cursors)
        assert all("error" not in event for event in second.json()["events"])

        invalid_cursor = client.get(
            "/api/admin/service/events?cursor=not-a-cursor", headers=headers
        )
        invalid_limit = client.get("/api/admin/service/events?limit=101", headers=headers)
        assert invalid_cursor.status_code == 400
        assert invalid_cursor.json()["detail"] == "invalid_cursor"
        assert invalid_limit.status_code == 400
        assert invalid_limit.json()["detail"] == "invalid_limit"

        audit = client.get("/api/admin/audit?action=service.reload", headers=headers)
        assert audit.status_code == 200
        assert audit.json()["events"][0]["action"] == "service.reload"

        operations = client.get(
            "/api/admin/service/events?event_type=operation&result=succeeded",
            headers=headers,
        )
        operations_by_status = client.get(
            "/api/admin/service/events?event_type=operation&status=succeeded",
            headers=headers,
        )
        assert operations.status_code == 200
        assert operations.json()["events"]
        assert all(event["event_type"] == "operation" for event in operations.json()["events"])
        assert all(event["status"] == "succeeded" for event in operations.json()["events"])
        assert all(event["in_flight"] == 0 for event in operations.json()["events"])
        assert operations_by_status.json()["events"] == operations.json()["events"]
        assert "error_message" not in operations.text

        states = client.get(
            "/api/admin/service/events?event_type=state",
            headers=headers,
        )
        assert states.status_code == 200
        assert states.json()["events"]
        assert all(event["in_flight"] == 0 for event in states.json()["events"])

        invalid_type = client.get(
            "/api/admin/service/events?event_type=lifecycle", headers=headers
        )
        invalid_result = client.get(
            "/api/admin/service/events?result=unknown", headers=headers
        )
        assert invalid_type.status_code == 400
        assert invalid_type.json()["detail"] == "invalid_event_type"
        assert invalid_result.status_code == 400
        assert invalid_result.json()["detail"] == "invalid_status"


def _supervisor(settings, **kwargs) -> ServiceSupervisor:
    return ServiceSupervisor(
        settings,
        process_factory=lambda command, env: _Process(),
        health_checker=lambda process: asyncio.sleep(0, result=True),
        signal_sender=lambda process, group, requested: process.terminate(),
        **kwargs,
    )


def test_failed_service_operation_is_audited_without_raw_error(tmp_path) -> None:
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
        worker_autostart=False,
    )

    def failing_supervisor(current, **kwargs) -> ServiceSupervisor:
        def fail_start(_command, _env):
            raise RuntimeError("worker-secret-must-not-leak")

        return ServiceSupervisor(current, process_factory=fail_start, **kwargs)

    app = create_control_app(lambda: settings, supervisor_factory=failing_supervisor)
    headers = {"Authorization": "Bearer admin-secret"}
    with TestClient(app) as client:
        response = client.post("/api/admin/service/start", headers=headers)
        events = client.get(
            "/api/admin/service/events?event_type=operation&result=failed",
            headers=headers,
        )
        audit = client.get(
            "/api/admin/audit?action=service.start&result=failed", headers=headers
        )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["error_code"] == "service_operation_failed"
    assert "error" not in response.json()
    assert events.json()["events"][0]["error_code"] == "service_operation_failed"
    assert audit.json()["events"][0]["result"] == "failed"
    assert "worker-secret-must-not-leak" not in response.text + events.text + audit.text
