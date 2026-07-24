"""Worker telemetry transport and Control Plane ingestion tests."""

from __future__ import annotations

import asyncio

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from qb2api.config import Settings
from qb2api.control.app import create_control_app
from qb2api.worker.telemetry import WorkerTelemetry


def test_control_ingests_only_internal_telemetry(tmp_path):
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        worker_internal_token="internal-token",
        data_dir=str(tmp_path),
    )
    event = {
        "event_id": "event-1",
        "request_id": "request-1",
        "provider": "codebuddy",
        "account_id": "cb-1",
        "model_id": "model-a",
        "protocol": "openai",
        "status": "succeeded",
        "stream_committed": True,
        "prompt": "must-be-dropped",
    }
    with TestClient(
        create_control_app(lambda: settings),
        client=("127.0.0.1", 10001),
    ) as client:
        denied = client.post("/api/control/telemetry", json={"events": [event]})
        assert denied.status_code == 401
        accepted = client.post(
            "/api/control/telemetry",
            headers={"X-QB2API-Worker-Token": "internal-token"},
            json={"events": [event]},
        )
        assert accepted.status_code == 200
        assert accepted.json()["accepted"] == 1
        stored = client.get("/api/admin/usage/events", headers={"Authorization": "Bearer admin-secret"})
        assert stored.status_code == 200
        assert "must-be-dropped" not in stored.text


async def _wait_for_sent(sent: list[list[dict]]) -> None:
    for _ in range(20):
        if sent:
            return
        await asyncio.sleep(0.01)


async def _transport_test() -> None:
    sent: list[list[dict]] = []

    async def sender(events):
        sent.append(events)

    telemetry = WorkerTelemetry(endpoint="http://127.0.0.1:1", token="token", sender=sender, queue_size=10)
    telemetry.start()
    telemetry.emit({"event_id": "e-1", "provider": "qoder", "prompt": "drop"})
    await _wait_for_sent(sent)
    await telemetry.stop()
    assert sent[0][0]["event_id"] == "e-1"
    assert "prompt" not in sent[0][0]


def test_worker_telemetry_sanitizes_and_flushes():
    asyncio.run(_transport_test())
