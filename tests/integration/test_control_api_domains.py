"""Control Plane domain API contracts."""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from qb2api.config import Settings
from qb2api.control.app import create_control_app


def test_settings_models_usage_metrics_and_audit_are_secret_safe(tmp_path) -> None:
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
    )
    headers = {"Authorization": "Bearer admin-secret"}
    with TestClient(create_control_app(lambda: settings)) as client:
        response = client.get("/api/admin/settings", headers=headers)
        assert response.status_code == 200
        assert response.json()["schema"]["checkin.at"]["type"] == "str"

        changed = client.patch(
            "/api/admin/settings",
            headers=headers,
            json={"key": "checkin.at", "value": "01:20", "value_version": 0},
        )
        assert changed.status_code == 200
        assert changed.json()["apply_status"] == "effective"
        assert client.app.state.settings.checkin_at == "01:20"
        invalid = client.patch(
            "/api/admin/settings",
            headers=headers,
            json={"key": "checkin.at", "value": "25:00", "value_version": 1},
        )
        assert invalid.status_code == 400
        assert client.app.state.settings.checkin_at == "01:20"

        assert client.get("/api/admin/models", headers=headers).json()["models"] == []
        assert client.get("/api/admin/usage/summary", headers=headers).json()["summary"]["request_count"] == 0
        assert client.get("/api/admin/metrics/accounts", headers=headers).json()["snapshots"] == []
        refreshed = client.post("/api/admin/metrics/refresh", headers=headers)
        assert refreshed.status_code == 202
        operation_id = refreshed.json()["operation_id"]
        operation = None
        for _ in range(20):
            operation = client.get(
                f"/api/admin/metrics/refresh/{operation_id}", headers=headers
            )
            if operation.json()["status"] != "running":
                break
        assert operation is not None
        assert operation.json()["status"] == "succeeded"
        assert operation.json()["result"]["fresh"] == 0
        audit = client.get("/api/admin/audit", headers=headers)
        assert audit.status_code == 200
        assert {event["action"] for event in audit.json()["events"]} >= {
            "settings.update",
            "metrics.refresh",
        }

        credentials = client.get("/api/admin/credentials", headers=headers)
        assert credentials.status_code == 200
        assert "encrypted_payload" not in credentials.text

        backup = client.post("/api/admin/backup", headers=headers)
        assert backup.status_code == 200
        backup_id = backup.json()["backup_id"]
        dry_run = client.post(
            f"/api/admin/backup/{backup_id}/restore",
            headers=headers,
            json={"dry_run": True},
        )
        assert dry_run.status_code == 200
        assert dry_run.json()["next_step"] == "offline_restore_required"


def test_setting_apply_failure_is_persisted_and_audited(tmp_path, monkeypatch) -> None:
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
    )
    headers = {"Authorization": "Bearer admin-secret"}
    with TestClient(create_control_app(lambda: settings)) as client:

        async def fail_apply(_key, _value):
            raise RuntimeError("runtime-secret-must-not-leak")

        monkeypatch.setattr(client.app.state.runtime, "apply_setting", fail_apply)
        response = client.patch(
            "/api/admin/settings",
            headers=headers,
            json={
                "key": "service.worker.autostart",
                "value": True,
                "value_version": 0,
            },
        )
        stored = client.get("/api/admin/settings", headers=headers)
        audit = client.get(
            "/api/admin/audit?action=settings.update&result=failed",
            headers=headers,
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "setting_apply_failed"
    item = next(
        value
        for value in stored.json()["settings"]
        if value["key"] == "service.worker.autostart"
    )
    assert item["value"] is True
    assert item["apply_status"] == "failed"
    assert item["last_error"] == "setting_apply_failed"
    assert audit.json()["events"][0]["error_code"] == "setting_apply_failed"
    assert "runtime-secret-must-not-leak" not in response.text + audit.text
