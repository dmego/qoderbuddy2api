"""Proxy API key lifecycle and Worker hash verification contracts."""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from qb2api.admin.crypto import hash_token
from qb2api.config import Settings
from qb2api.control.app import create_control_app
from qb2api.models import load_models_from_config
from qb2api.runtime_snapshot import RuntimeProxyKey, RuntimeSlot, RuntimeSnapshot
from qb2api.worker.app import create_worker_app


def test_proxy_key_is_revealed_once_and_revoke_is_secret_safe(tmp_path) -> None:
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
    )
    headers = {"Authorization": "Bearer admin-secret"}
    with TestClient(create_control_app(lambda: settings)) as client:
        created = client.post("/api/admin/proxy-keys", headers=headers, json={"name": "Claude Code"})
        key_id = created.json()["key_id"]
        listed = client.get("/api/admin/proxy-keys", headers=headers)
        revoked = client.post(f"/api/admin/proxy-keys/{key_id}/revoke", headers=headers)
        listed_after = client.get("/api/admin/proxy-keys", headers=headers)

    raw_key = created.json()["key"]
    assert created.status_code == 201
    assert raw_key.startswith("qb2api_")
    assert raw_key not in listed.text
    assert listed.json()["keys"][0]["key_id"] == key_id
    assert revoked.status_code == 200
    assert listed_after.json()["keys"][0]["enabled"] is False
    assert raw_key not in listed_after.text


def test_worker_accepts_only_hashes_from_runtime_snapshot(tmp_path) -> None:
    settings = Settings(data_dir=str(tmp_path))
    snapshot = RuntimeSnapshot(
        snapshot_version=1,
        codebuddy_endpoint=settings.codebuddy_endpoint,
        qoder_timeout=settings.qoder_timeout,
        models=load_models_from_config(settings.model_config_path),
        slots=(RuntimeSlot("codebuddy", "cb-1", 1, "ck-runtime"),),
        proxy_keys=(RuntimeProxyKey("pk-1", hash_token("runtime-proxy")),),
    )

    async def load_snapshot() -> RuntimeSnapshot:
        return snapshot

    with TestClient(create_worker_app(lambda: settings, snapshot_loader=load_snapshot)) as client:
        accepted = client.get("/v1/models", headers={"Authorization": "Bearer runtime-proxy"})
        rejected = client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
        admin_rejected = client.get("/v1/models", headers={"Authorization": "Bearer admin-secret"})

    assert accepted.status_code == 200
    assert rejected.status_code == 401
    assert admin_rejected.status_code == 401


def test_proxy_key_rotation_atomically_replaces_active_key(tmp_path) -> None:
    settings = Settings(
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
    )
    headers = {"Authorization": "Bearer admin-secret"}
    with TestClient(create_control_app(lambda: settings)) as client:
        created = client.post("/api/admin/proxy-keys", headers=headers, json={"name": "Codex"})
        rotated = client.post(
            f"/api/admin/proxy-keys/{created.json()['key_id']}/rotate",
            headers=headers,
        )
        listed = client.get("/api/admin/proxy-keys", headers=headers)

    assert rotated.status_code == 201
    assert rotated.json()["key"].startswith("qb2api_")
    assert rotated.json()["key"] != created.json()["key"]
    states = {item["key_id"]: item["enabled"] for item in listed.json()["keys"]}
    assert states[created.json()["key_id"]] is False
    assert states[rotated.json()["key_id"]] is True
    assert rotated.json()["key"] not in listed.text
