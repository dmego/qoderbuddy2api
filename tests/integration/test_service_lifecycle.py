"""Control Plane service lifecycle API contract."""

from __future__ import annotations

import asyncio
import sqlite3

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from qb2api.config import Settings
from qb2api.control.app import create_control_app
from qb2api.control.supervisor import ServiceSupervisor


class _Process:
    pid = 4812
    start_time = 1.0
    process_group_id = 4812

    def __init__(self) -> None:
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode


def test_service_status_and_idempotent_start_contract(tmp_path) -> None:
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
        worker_autostart=False,
    )
    with TestClient(create_control_app(lambda: settings, supervisor_factory=_supervisor)) as client:
        headers = {"Authorization": "Bearer admin-secret"}
        status = client.get("/api/admin/service", headers=headers)
        assert status.status_code == 200
        assert status.json()["service"] == "proxy-worker"
        assert status.json()["observed_state"] == "STOPPED"

        started = client.post(
            "/api/admin/service/start",
            headers={**headers, "Idempotency-Key": "start-once"},
        )
        assert started.status_code == 200
        assert started.json()["status"] == "succeeded"
        assert "operation_id" in started.json()

    with sqlite3.connect(tmp_path / "qb2api.sqlite3") as database:
        persisted = database.execute(
            "SELECT observed_state FROM service_runtime WHERE service_name='proxy-worker'"
        ).fetchone()
        operation = database.execute(
            "SELECT status FROM service_operations WHERE operation_id=?",
            (started.json()["operation_id"],),
        ).fetchone()
    assert persisted == ("STOPPED",)
    assert operation == ("succeeded",)


def _supervisor(settings, **kwargs) -> ServiceSupervisor:
    return ServiceSupervisor(
        settings,
        process_factory=lambda command, env: _Process(),
        health_checker=lambda process: asyncio.sleep(0, result=True),
        signal_sender=lambda process, group, requested: process.terminate(),
        **kwargs,
    )
