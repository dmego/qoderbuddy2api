"""Launch an isolated Control Plane for browser acceptance tests.

This intentionally constructs Settings rather than calling Settings.from_env(),
so Playwright cannot discover a developer's local .env credentials.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from pathlib import Path

import uvicorn
from cryptography.fernet import Fernet

from qb2api.config import Settings
from qb2api.control.app import create_control_app

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _data_dir() -> Path:
    configured = os.getenv("QB2API_E2E_DATA_DIR")
    if configured:
        return Path(configured)
    directory = Path(tempfile.mkdtemp(prefix="qb2api-playwright-"))
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    return directory


def _settings() -> Settings:
    data_dir = _data_dir()
    return Settings(
        control_host="127.0.0.1",
        control_port=int(os.getenv("QB2API_E2E_CONTROL_PORT", "19299")),
        worker_host="127.0.0.1",
        worker_port=int(os.getenv("QB2API_E2E_WORKER_PORT", "19301")),
        worker_autostart=False,
        worker_shutdown_timeout_seconds=2,
        proxy_api_key="playwright-proxy-key",
        admin_key="playwright-admin-key",
        credential_key=Fernet.generate_key().decode(),
        data_dir=str(data_dir),
        admin_ui_enabled=True,
        admin_cookie_secure="false",
        model_config_path=os.getenv(
            "QB2API_E2E_MODEL_CONFIG", str(PROJECT_ROOT / "config" / "models.json")
        ),
        log_dir=str(data_dir / "logs"),
    )


def main() -> None:
    settings = _settings()
    application = create_control_app(lambda: settings)
    uvicorn.run(
        application,
        host=settings.control_host,
        port=settings.control_port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
