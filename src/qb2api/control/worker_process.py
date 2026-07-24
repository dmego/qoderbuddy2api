"""Worker command, environment, and subprocess helpers."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

from qb2api.config import Settings


def worker_command(settings: Settings) -> list[str]:
    return [
        sys.executable, "-m", "uvicorn", "qb2api.worker.app:app",
        "--host", settings.worker_host, "--port", str(settings.worker_port),
    ]


def worker_environment(settings: Settings, owner: str, auth_version: int) -> dict[str, str]:
    control_host = settings.control_host
    if control_host in {"0.0.0.0", "::"}:
        control_host = "127.0.0.1"
    environment = {
        "QB2API_WORKER_OWNER_INSTANCE_ID": owner,
        "QB2API_WORKER_INTERNAL_AUTH_VERSION": str(auth_version),
        "QB2API_CONTROL_HOST": control_host,
        "QB2API_CONTROL_PORT": str(settings.control_port),
        "QB2API_WORKER_HOST": settings.worker_host,
        "QB2API_WORKER_PORT": str(settings.worker_port),
        "QB2API_LOG_LEVEL": settings.log_level,
        "QB2API_LOG_DIR": settings.log_dir,
        "QB2API_LOG_REQUESTS": "1" if settings.log_requests else "0",
        "QB2API_ADMIN_UI_ENABLED": "0",
        "QB2API_ADMIN_KEY": "",
        "QB2API_CREDENTIAL_KEY": "",
        "QB2API_PROXY_API_KEY": "",
        "CODEBUDDY_TOKEN": "",
        "QODER_TOKEN": "",
    }
    if settings.worker_internal_token:
        environment["QB2API_WORKER_INTERNAL_TOKEN"] = settings.worker_internal_token
    return environment


def spawn_worker(command: list[str], environment: dict[str, str]) -> subprocess.Popen:
    merged = dict(os.environ)
    merged.update(environment)
    return subprocess.Popen(command, env=merged, start_new_session=os.name != "nt")


def wait_process(process: Any, deadline: float) -> bool:
    if process is None:
        return True
    remaining = max(0.0, deadline - time.monotonic())
    try:
        process.wait(timeout=remaining)
        return True
    except subprocess.TimeoutExpired:
        return process.poll() is not None
