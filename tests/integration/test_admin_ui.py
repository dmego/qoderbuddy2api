"""Admin UI shell, asset, and authentication boundary contracts."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from qb2api.config import Settings
from qb2api.control.app import create_control_app as create_app


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
    assert client.get("/admin/assets/admin.js.map").status_code == 404


def test_public_shell_loads_but_account_state_remains_private() -> None:
    client = _client(enabled=True)

    shell = client.get("/admin")
    accounts = client.get("/api/admin/accounts")

    assert shell.status_code == 200
    assert "2api Admin" in shell.text
    assert accounts.status_code == 401
    assert "admin-secret" not in shell.text


def test_admin_shell_references_loadable_es_module_assets() -> None:
    import re

    client = _client(enabled=True)
    shell = client.get("/admin")
    assert shell.status_code == 200
    assert 'type="module"' in shell.text
    assert client.get("/admin/assets/admin.js").status_code == 200
    css_asset = re.search(r'href="/admin/assets/([^"]+\.css)"', shell.text)
    assert css_asset is not None
    assert client.get(f"/admin/assets/{css_asset.group(1)}").status_code == 200
    assert client.get("/admin/assets/admin.js.map").status_code == 404
    assert "admin/assets/admin.js" in shell.text


def test_admin_assets_disable_stale_entrypoint_cache() -> None:
    import re

    client = _client(enabled=True)
    shell = client.get("/admin").text
    css_asset = re.search(r'href="/admin/assets/([^"]+\.css)"', shell)
    assert css_asset is not None
    for asset in ("admin.js", css_asset.group(1)):
        response = client.get(f"/admin/assets/{asset}")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate"
        assert response.headers["pragma"] == "no-cache"


def test_packaged_ui_has_no_legacy_assets_or_production_source_maps() -> None:
    web_root = Path(__file__).parents[2] / "src" / "qb2api" / "web"
    assets = web_root / "dist" / "assets"

    assert (web_root / "dist" / "index.html").is_file()
    assert list(assets.glob("*.css"))
    assert (assets / "admin.js").is_file()
    assert not list(assets.glob("*.map"))
    assert "sourceMappingURL" not in (assets / "admin.js").read_text()
    for legacy_name in ("admin.html", "admin.js", "admin.css"):
        assert not (web_root / legacy_name).exists()


def test_admin_modules_do_not_use_inner_html_for_rendering() -> None:
    web_dir = Path(__file__).parents[2] / "frontend" / "src"

    for script in [*web_dir.rglob("*.ts"), *web_dir.rglob("*.vue")]:
        assert ".innerHTML" not in script.read_text(), script.name
