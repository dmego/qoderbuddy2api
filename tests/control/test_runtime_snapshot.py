"""Versioned Control/Worker runtime snapshot contracts."""

from __future__ import annotations

import json

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from qb2api.accounts.repository import AccountRepository
from qb2api.config import Settings
from qb2api.control.app import create_control_app
from qb2api.control.runtime_snapshot import RuntimeSnapshotService
from qb2api.control.service_models import WorkerIdentity
from qb2api.models import ModelCapabilities, ModelDefinition, load_models_from_config
from qb2api.runtime import RuntimeServices
from qb2api.runtime_snapshot import (
    RUNTIME_PROTOCOL_VERSION,
    RuntimeSlot,
    RuntimeSnapshot,
    _model_payload,
    _parse_model,
)
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
    assert accepted.json()["proxy_auth_required"] is False
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
    assert any(item["id"] == "auto" for item in models.json()["data"])
    assert not (tmp_path / "qb2api.sqlite3").exists()


async def test_snapshot_qoder_models_come_only_from_upstream(tmp_path) -> None:
    config_path = tmp_path / "models.json"
    config_path.write_text(
        json.dumps(
            {
                "qoder": {
                    "models": [
                        {"id": "Qwen3.8-Max", "name": "Qwen 3.8 Max (config)", "max_context": 64000},
                        {"id": "Config-Only", "name": "Config Only", "max_context": 100000},
                    ]
                }
            }
        )
    )
    settings = Settings(
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
        model_config_path=str(config_path),
    )
    repository = AccountRepository(str(tmp_path / "catalog.sqlite3"))
    await repository.connect()
    await repository.migrate()
    try:
        await repository.upsert_model(
            provider="qoder",
            model_id="Qwen3.8-Max",
            display_name="Qwen3.8-Max",
            capabilities=["chat", "streaming", "reasoning", "reasoning_effort", "context_window"],
            source="upstream",
            enabled=True,
            metadata={
                "cosy_key": "qmodel_38max",
                "default_context_window": 131072,
                "default_effort": "high",
            },
        )
        runtime = RuntimeServices(settings)
        runtime.account_repo = repository
        snapshot = await RuntimeSnapshotService(runtime).build()
    finally:
        await repository.close()

    models = {model.id: model for model in snapshot.models["qoder"]}
    assert set(models) == {"Qwen3.8-Max"}  # stale config baseline is dropped
    upstream = models["Qwen3.8-Max"]
    assert upstream.provider == "qoder"
    assert upstream.name == "Qwen3.8-Max"
    assert upstream.max_context == 131072
    assert upstream.max_output == 4096
    assert upstream.capabilities.reasoning is True
    assert upstream.metadata == {"cosy_key": "qmodel_38max", "default_effort": "high"}

    # Merged snapshot must survive the worker-side payload roundtrip.
    restored = RuntimeSnapshot.from_payload(snapshot.to_payload())
    restored_models = {model.id: model for model in restored.models["qoder"]}
    assert set(restored_models) == {"Qwen3.8-Max"}
    assert restored_models["Qwen3.8-Max"].metadata == upstream.metadata
    assert restored_models["Qwen3.8-Max"].max_context == 131072


def test_model_payload_roundtrip_carries_metadata() -> None:
    model = ModelDefinition(
        id="Qwen3.8-Max",
        name="Qwen 3.8 Max",
        provider="qoder",
        capabilities=ModelCapabilities(chat=True, streaming=True, reasoning=True),
        max_context=131072,
        max_output=4096,
        metadata={"cosy_key": "qmodel_38max", "default_effort": "high"},
    )
    payload = _model_payload(model)
    assert payload["metadata"] == {"cosy_key": "qmodel_38max", "default_effort": "high"}
    parsed = _parse_model("qoder", payload)
    assert parsed.id == model.id
    assert parsed.name == model.name
    assert parsed.max_context == 131072
    assert parsed.max_output == 4096
    assert parsed.capabilities.reasoning is True
    assert parsed.metadata == model.metadata


def test_model_payload_without_metadata_parses_cleanly() -> None:
    model = ModelDefinition(id="legacy-model", name="Legacy Model", provider="qoder")
    payload = _model_payload(model)
    assert "metadata" not in payload
    parsed = _parse_model("qoder", payload)
    assert parsed.metadata is None
    assert parsed.max_context == 128000
    assert parsed.max_output == 4096


def _worker_headers() -> dict[str, str]:
    return {"X-QB2API-Worker-Token": "internal-secret"}


def _handshake(owner: str, auth_version: int) -> dict[str, object]:
    return {
        "protocol_version": RUNTIME_PROTOCOL_VERSION,
        "owner_instance_id": owner,
        "internal_auth_version": auth_version,
    }
