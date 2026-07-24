"""Control Plane and Proxy Worker process-boundary contracts."""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from qb2api.app import app as compatibility_app
from qb2api.app import create_app
from qb2api.config import Settings
from qb2api.control.app import create_control_app
from qb2api.worker.app import create_worker_app


def test_control_plane_does_not_expose_proxy_routes(tmp_path) -> None:
    settings = Settings(
        admin_ui_enabled=True,
        admin_key="admin-secret",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(tmp_path),
    )
    with TestClient(create_control_app(lambda: settings)) as client:
        assert client.get("/health").json()["component"] == "control-plane"
        assert client.get("/v1/models").status_code == 404
        assert client.get("/admin").status_code == 200


def test_compatibility_app_defaults_to_the_control_plane() -> None:
    assert compatibility_app.state.role == "control"
    assert create_app is create_control_app


def test_worker_blocks_management_surface() -> None:
    settings = Settings(codebuddy_tokens=["ck-worker"])
    with TestClient(create_worker_app(lambda: settings)) as client:
        assert client.get("/internal/health/live").json()["component"] == "proxy-worker"
        assert client.get("/admin").status_code == 404
        assert client.get("/api/config").status_code == 404


def test_worker_owns_proxy_routes_without_legacy_subapplication() -> None:
    settings = Settings(codebuddy_tokens=["ck-worker"])
    worker = create_worker_app(lambda: settings)

    assert all(getattr(route, "name", None) != "proxy" for route in worker.routes)
    with TestClient(worker) as client:
        response = client.get("/v1/models")

    assert response.status_code == 200
    assert response.json()["object"] == "list"


def test_worker_reports_invalid_openai_json_as_client_error() -> None:
    settings = Settings(codebuddy_tokens=["ck-worker"], proxy_api_key="proxy-secret")

    with TestClient(create_worker_app(lambda: settings)) as client:
        response = client.post(
            "/v1/chat/completions",
            content=b"{invalid json",
            headers={"Authorization": "Bearer proxy-secret"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
