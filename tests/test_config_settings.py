"""Tests for Settings env binding, proxy key alias, and startup validation."""

from __future__ import annotations

import pytest

from qb2api.config import Settings


def _clear_relevant_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "QB2API_PROXY_API_KEY",
        "QB2API_API_KEY",
        "QB2API_ADMIN_KEY",
        "QB2API_CREDENTIAL_KEY",
        "QB2API_ADMIN_UI_ENABLED",
        "QB2API_DATA_DIR",
        "CHECKIN_ENABLED",
        "CODEBUDDY_CHECKIN_ENABLED",
        "QODER_CHECKIN_ENABLED",
        "CODEBUDDY_TOKEN",
        "QODER_TOKEN",
        "CODEBUDDY_CREDITS_PATH",
        "QB2API_METRICS_HISTORY_RETENTION_DAYS",
    ):
        monkeypatch.delenv(key, raising=False)


def _settings_from_test_env() -> Settings:
    """Bind only monkeypatched process values, never this checkout's real .env."""
    return Settings.from_env(env_file="")


def test_proxy_key_prefers_new_env(monkeypatch: pytest.MonkeyPatch):
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("QB2API_PROXY_API_KEY", "proxy-new")
    monkeypatch.setenv("QB2API_API_KEY", "proxy-legacy")
    monkeypatch.setenv("QB2API_ADMIN_UI_ENABLED", "false")

    settings = _settings_from_test_env()
    assert settings.proxy_api_key == "proxy-new"
    assert settings.api_key == "proxy-new"


def test_proxy_key_falls_back_to_legacy_alias(monkeypatch: pytest.MonkeyPatch):
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("QB2API_API_KEY", "legacy-only")
    monkeypatch.setenv("QB2API_ADMIN_UI_ENABLED", "false")

    settings = _settings_from_test_env()
    assert settings.proxy_api_key == "legacy-only"
    assert settings.api_key == "legacy-only"


def test_legacy_api_key_property_maps_to_proxy(monkeypatch: pytest.MonkeyPatch):
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("QB2API_ADMIN_UI_ENABLED", "false")
    settings = _settings_from_test_env()
    assert settings.api_key is None

    constructed = Settings(proxy_api_key="p1", admin_ui_enabled=False)
    assert constructed.api_key == "p1"
    assert constructed.proxy_api_key == "p1"


def test_checkin_defaults_off_unless_provider_flags(monkeypatch: pytest.MonkeyPatch):
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("QB2API_ADMIN_UI_ENABLED", "false")
    settings = _settings_from_test_env()
    assert settings.checkin_enabled is False
    assert settings.codebuddy_checkin_enabled is False
    assert settings.qoder_checkin_enabled is False

    monkeypatch.setenv("CODEBUDDY_CHECKIN_ENABLED", "true")
    settings = _settings_from_test_env()
    assert settings.codebuddy_checkin_enabled is True
    assert settings.checkin_enabled is True


def test_credits_path_and_history_retention_defaults(monkeypatch: pytest.MonkeyPatch):
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("QB2API_ADMIN_UI_ENABLED", "false")
    settings = _settings_from_test_env()
    assert settings.codebuddy_credits_path == "/billing/meter/get-user-resource"
    assert settings.metrics_history_retention_days == 90

    monkeypatch.setenv("QB2API_METRICS_HISTORY_RETENTION_DAYS", "180")
    monkeypatch.setenv("CODEBUDDY_CREDITS_PATH", "/custom/credits")
    settings = _settings_from_test_env()
    assert settings.metrics_history_retention_days == 180
    assert settings.codebuddy_credits_path == "/custom/credits"


def test_validate_rejects_equal_proxy_and_admin_keys():
    settings = Settings(
        proxy_api_key="same",
        admin_key="same",
        admin_ui_enabled=False,
        credential_key=None,
    )
    with pytest.raises(ValueError, match="must be different"):
        settings.validate_startup()


def test_validate_requires_admin_and_credential_when_admin_ui():
    settings = Settings(
        proxy_api_key="proxy",
        admin_key=None,
        admin_ui_enabled=True,
        credential_key=None,
    )
    with pytest.raises(ValueError, match="QB2API_ADMIN_KEY"):
        settings.validate_startup()

    settings = Settings(
        proxy_api_key="proxy",
        admin_key="admin",
        admin_ui_enabled=True,
        credential_key=None,
    )
    with pytest.raises(ValueError, match="QB2API_CREDENTIAL_KEY"):
        settings.validate_startup()


def test_validate_ok_for_legacy_open_proxy():
    settings = Settings(
        proxy_api_key=None,
        admin_key=None,
        admin_ui_enabled=False,
        checkin_enabled=False,
        credential_key=None,
    )
    settings.validate_startup()  # no raise


def test_validate_ok_when_keys_differ_and_admin_configured():
    settings = Settings(
        proxy_api_key="proxy-key",
        admin_key="admin-key",
        admin_ui_enabled=True,
        credential_key="not-a-real-fernet-but-present",
    )
    settings.validate_startup()


def test_design_checkin_path_defaults(monkeypatch: pytest.MonkeyPatch):
    _clear_relevant_env(monkeypatch)
    monkeypatch.setenv("QB2API_ADMIN_UI_ENABLED", "false")
    settings = _settings_from_test_env()
    assert settings.codebuddy_checkin_base == "https://www.workbuddy.cn"
    assert settings.codebuddy_checkin_status_path == "/billing/meter/checkin-status"
    assert settings.codebuddy_checkin_status_method == ""
    assert settings.codebuddy_checkin_claim_path == "/billing/meter/daily-checkin"
    assert settings.codebuddy_checkin_claim_method == "POST"
    assert settings.qoder_checkin_base == "https://openapi.qoder.com.cn"
    assert settings.qoder_activity_base == "https://gateway.qoder.com.cn"
    assert settings.qoder_checkin_status_path == "/sash/api/v1/me/daily-check-in/status"
    assert settings.qoder_checkin_claim_path == "/sash/api/v1/me/daily-check-in/claim"
    assert settings.qoder_checkin_refresh_path == "/api/v1/deviceToken/refresh"
    assert settings.qoder_quota_path == "/api/v2/quota/usage"
    assert settings.qoder_activity_path == "/algo/api/v2/activity"
    assert settings.checkin_at == "00:10"
    assert settings.checkin_timezone == "Asia/Shanghai"
    assert settings.data_dir == "./data"
    assert settings.provider_drain_timeout_seconds == 330
