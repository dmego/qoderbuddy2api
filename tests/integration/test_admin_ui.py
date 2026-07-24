"""Admin UI shell, asset, and authentication boundary contracts."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from qb2api.app import create_app
from qb2api.config import Settings


def _client(*, enabled: bool) -> TestClient:
    settings = Settings(
        admin_ui_enabled=enabled,
        admin_key="admin-secret",
        credential_key="test-only-key",
    )
    return TestClient(create_app(lambda: settings))


def test_disabled_admin_ui_hides_shell_and_assets() -> None:
    client = _client(enabled=False)

    assert client.get("/admin").status_code == 404
    assert client.get("/admin/assets/admin.css").status_code == 404
    assert client.get("/admin/assets/admin.js").status_code == 404


def test_public_shell_loads_but_account_state_remains_private() -> None:
    client = _client(enabled=True)

    shell = client.get("/admin")
    accounts = client.get("/api/admin/accounts")

    assert shell.status_code == 200
    assert "2api Admin" in shell.text
    assert accounts.status_code == 401
    assert "admin-secret" not in shell.text


def test_admin_shell_references_loadable_es_module_assets() -> None:
    client = _client(enabled=True)
    shell = client.get("/admin").text

    assert 'type="module"' in shell
    assert client.get("/admin/assets/admin.css").status_code == 200
    assert client.get("/admin/assets/admin.js").status_code == 200
    assert "admin/assets/admin.js" in shell


def test_admin_modules_do_not_use_inner_html_for_rendering() -> None:
    web_dir = Path(__file__).parents[2] / "frontend" / "src"

    for script in [*web_dir.rglob("*.ts"), *web_dir.rglob("*.vue")]:
        assert ".innerHTML" not in script.read_text(), script.name
