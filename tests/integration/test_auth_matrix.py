"""End-to-end authentication matrix for proxy and admin surfaces."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from qb2api.admin.auth import AdminSessionStore, LoginRateLimiter
from qb2api.config import Settings
from qb2api.control.app import create_control_app


def test_proxy_key_never_authorizes_the_management_api() -> None:
    configured = Settings(
        proxy_api_key="proxy-secret",
        admin_key="admin-secret",
        admin_ui_enabled=False,
    )
    application = create_control_app(lambda: configured)

    response = TestClient(application).get(
        "/api/admin/accounts",
        headers={"Authorization": "Bearer proxy-secret"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_legacy_config_is_admin_only_and_never_echoes_keys() -> None:
    configured = Settings(
        proxy_api_key="proxy-secret",
        admin_key="admin-secret",
        admin_ui_enabled=False,
    )
    application = create_control_app(lambda: configured)

    proxy_response = TestClient(application).get(
        "/api/config",
        headers={"Authorization": "Bearer proxy-secret"},
    )
    admin_response = TestClient(application).get(
        "/api/config",
        headers={"Authorization": "Bearer admin-secret"},
    )
    update_response = TestClient(application).patch(
        "/api/config",
        headers={"Authorization": "Bearer admin-secret"},
        json={"log_level": "debug"},
    )

    assert proxy_response.status_code == 403
    assert admin_response.status_code == 200
    assert "proxy-secret" not in admin_response.text
    assert "admin-secret" not in admin_response.text
    assert update_response.status_code == 410


def test_untrusted_peer_cannot_spoof_forwarded_https() -> None:
    configured = Settings(
        admin_key="admin-secret",
        admin_ui_enabled=True,
        credential_key="configured-for-startup-only",
        admin_cookie_secure="auto",
        trusted_proxy_headers=True,
        trusted_proxy_networks=["10.0.0.0/8"],
    )
    application = create_control_app(lambda: configured)
    application.state.admin_sessions = AdminSessionStore()
    application.state.login_limiter = LoginRateLimiter()

    response = TestClient(application).post(
        "/api/admin/session",
        json={"admin_key": "admin-secret"},
        headers={"X-Forwarded-Proto": "https"},
    )

    assert response.status_code == 400
    assert "requires HTTPS" in response.json()["error"]


@pytest.mark.asyncio
async def test_reauthentication_revokes_previous_cookie_session() -> None:
    configured = Settings(
        admin_key="admin-secret",
        admin_ui_enabled=True,
        credential_key="configured-for-startup-only",
        admin_cookie_secure="true",
    )
    store = AdminSessionStore()
    application = create_control_app(lambda: configured)
    application.state.admin_sessions = store
    application.state.login_limiter = LoginRateLimiter()

    client = TestClient(application, base_url="https://testserver")
    first = client.post(
        "/api/admin/session",
        json={"admin_key": "admin-secret"},
    )
    previous = client.cookies.get("qb2api_admin_session")
    second = client.post(
        "/api/admin/session",
        json={"admin_key": "admin-secret"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert previous is not None
    assert client.cookies.get("qb2api_admin_session") != previous
    assert await store.validate_session(previous) is None
