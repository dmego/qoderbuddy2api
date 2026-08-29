"""Usage query, detail, and export contracts for the Control Plane."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from qb2api.config import Settings
from qb2api.control.app import create_control_app


@pytest.fixture
def usage_client(tmp_path):
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        worker_internal_token="internal-token",
        data_dir=str(tmp_path),
    )
    application = create_control_app(lambda: settings)
    with TestClient(application, client=("127.0.0.1", 10001)) as client:
        now = datetime.now(UTC).replace(microsecond=0)
        events = [
            _event("event-q1", now, "qoder", "qd-1", "model-a", 4, 3, reasoning_effort="low"),
            _event("event-q2", now, "qoder", "qd-2", "model-b", 5, 2),
            _event("event-c1", now, "codebuddy", "cb-1", "model-c", None, None),
        ]
        response = client.post(
            "/api/control/telemetry",
            headers={"X-QB2API-Worker-Token": "internal-token"},
            json={"events": events},
        )
        assert response.status_code == 200
        yield client, now


def test_usage_events_support_compound_filters(usage_client) -> None:
    client, now = usage_client
    query = (
        "provider=qoder&account_id=qd-1&model_id=model-a"
        f"&started_after={now.isoformat()}"
        f"&started_before={(now + timedelta(seconds=1)).isoformat()}"
    )
    response = client.get(f"/api/admin/usage/events?{query}", headers=_headers())

    assert response.status_code == 200
    assert [item["event_id"] for item in response.json()["events"]] == ["event-q1"]


def test_usage_event_detail_is_secret_safe(usage_client) -> None:
    client, _ = usage_client
    response = client.get("/api/admin/usage/events/event-q1", headers=_headers())

    assert response.status_code == 200
    assert response.json()["event_id"] == "event-q1"
    assert response.json()["reasoning_effort"] == "low"
    assert "prompt" not in response.text
    assert "redacted_error" not in response.json()


def test_usage_timeseries_returns_filtered_rollups(usage_client) -> None:
    client, _ = usage_client
    rolled = client.post("/api/admin/usage/rollup", headers=_headers())
    response = client.get(
        "/api/admin/usage/timeseries?bucket_kind=minute&provider=qoder&account_id=qd-1&model_id=model-a",
        headers=_headers(),
    )

    assert rolled.status_code == 200
    assert response.status_code == 200
    assert response.json()["rollups"][0]["request_count"] == 1


def test_usage_export_is_csv_and_audited(usage_client) -> None:
    client, _ = usage_client
    response = client.get(
        "/api/admin/usage/export?provider=qoder&account_id=qd-1",
        headers=_headers(),
    )
    audit = client.get("/api/admin/audit?limit=10", headers=_headers())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "reasoning_effort" in response.text
    assert "event-q1" in response.text
    assert "event-q2" not in response.text
    assert any(item["action"] == "usage.export" for item in audit.json()["events"])


def _event(event_id, started_at, provider, account_id, model_id, input_tokens, output_tokens, reasoning_effort=None):
    return {
        "event_id": event_id,
        "request_id": f"request-{event_id}",
        "provider": provider,
        "account_id": account_id,
        "model_id": model_id,
        "protocol": "openai",
        "status": "succeeded",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "started_at": started_at.isoformat(),
        "reasoning_effort": reasoning_effort,
    }


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}
