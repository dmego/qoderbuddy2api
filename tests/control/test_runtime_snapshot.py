"""Versioned Control/Worker runtime snapshot contracts."""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from qb2api.config import Settings
from qb2api.control.app import create_control_app
from qb2api.control.service_models import WorkerIdentity
from qb2api.models import load_models_from_config
from qb2api.runtime_snapshot import RUNTIME_PROTOCOL_VERSION, RuntimeSlot, RuntimeSnapshot
from qb2api.worker.app import create_worker_app


def test_control_handshake_returns_snapshot_only_to_owned_worker(tmp_path) -> None:
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        worker_internal_token="internal-secret",
        data_dir=str(tmp_path),
        qoder_tokens=["pt-env-secret"],
    )
    application = create_control_app(lambda: settings)
    with TestClient(application, client=("127.0.0.1", 10001)) as client:
        application.state.supervisor._snapshot.identity = WorkerIdentity(  # noqa: SLF001
            42, 1.0, 42, "owner-1", 7
        )
        rejected = client.post(
            "/api/control/worker/handshake",
            headers=_worker_headers(),
            json=_handshake("wrong-owner", 7),
        )
        accepted = client.post(
            "/api/control/worker/handshake",
            headers=_worker_headers(),
            json=_handshake("owner-1", 7),
        )

    assert rejected.status_code == 409
    assert accepted.status_code == 200
    assert accepted.json()["protocol_version"] == RUNTIME_PROTOCOL_VERSION
    assert accepted.json()["snapshot_version"] >= 1
    assert accepted.json()["slots"] == [
        {
            "provider": "qoder",
            "account_id": "qd-env-0",
            "credential_version": 1,
            "token": "pt-env-secret",
        }
    ]


def test_worker_boots_from_snapshot_without_opening_sqlite(tmp_path) -> None:
    settings = Settings(
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
    )
    snapshot = RuntimeSnapshot(
        snapshot_version=4,
        codebuddy_endpoint=settings.codebuddy_endpoint,
        qoder_timeout=settings.qoder_timeout,
        models=load_models_from_config(settings.model_config_path),
        slots=(RuntimeSlot("codebuddy", "cb-1", 3, "ck-runtime-secret"),),
    )

    async def load_snapshot() -> RuntimeSnapshot:
        return snapshot

    application = create_worker_app(lambda: settings, snapshot_loader=load_snapshot)
    with TestClient(application) as client:
        ready = client.get("/internal/health/ready")
        models = client.get("/v1/models")

    assert ready.status_code == 200
    assert ready.json()["snapshot_version"] == 4
    assert models.status_code == 200
    assert any(item["id"] == "codebuddy/auto" for item in models.json()["data"])
    assert not (tmp_path / "qb2api.sqlite3").exists()


def _worker_headers() -> dict[str, str]:
    return {"X-QB2API-Worker-Token": "internal-secret"}


def _handshake(owner: str, auth_version: int) -> dict[str, object]:
    return {
        "protocol_version": RUNTIME_PROTOCOL_VERSION,
        "owner_instance_id": owner,
        "internal_auth_version": auth_version,
    }
