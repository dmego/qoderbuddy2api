"""Real TCP smoke for the Control-owned supervised Worker runtime."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet


def test_supervised_worker_handshake_loads_control_snapshot(tmp_path) -> None:
    control_port, worker_port = _free_port(), _free_port()
    while worker_port == control_port:
        worker_port = _free_port()
    environment = _environment(tmp_path, control_port, worker_port)
    process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "qb2api.control.app:app",
            "--host", "127.0.0.1", "--port", str(control_port),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    output = ""
    try:
        control = _wait_json(f"http://127.0.0.1:{control_port}/health")
        ready = _wait_json(
            f"http://127.0.0.1:{worker_port}/internal/health/ready",
            {"X-QB2API-Worker-Token": "internal-smoke"},
        )
        models = _wait_json(
            f"http://127.0.0.1:{worker_port}/v1/models",
        )
        dynamic_key = _create_proxy_key(control_port)
        anonymous_after_create = httpx.get(
            f"http://127.0.0.1:{worker_port}/v1/models",
            timeout=5,
            trust_env=False,
        )
        dynamic_models = _wait_json(
            f"http://127.0.0.1:{worker_port}/v1/models",
            {"Authorization": f"Bearer {dynamic_key['key']}"},
        )
        revoked = _revoke_proxy_key(control_port, dynamic_key["key_id"])
        rejected = httpx.get(
            f"http://127.0.0.1:{worker_port}/v1/models",
            headers={"Authorization": f"Bearer {dynamic_key['key']}"},
            timeout=5,
            trust_env=False,
        )
        anonymous_after_revoke = httpx.get(
            f"http://127.0.0.1:{worker_port}/v1/models",
            timeout=5,
            trust_env=False,
        )
        reloaded = httpx.post(
            f"http://127.0.0.1:{control_port}/api/admin/service/reload",
            headers={"Authorization": "Bearer admin-smoke"},
            timeout=5,
            trust_env=False,
        )
        assert control["component"] == "control-plane"
        assert ready["snapshot_version"] >= 1
        assert any(item["id"] == "codebuddy/auto" for item in models["data"])
        assert anonymous_after_create.status_code == 401
        assert any(item["id"] == "codebuddy/auto" for item in dynamic_models["data"])
        assert revoked["status"] == "succeeded"
        assert rejected.status_code == 401
        assert anonymous_after_revoke.status_code == 401
        assert reloaded.status_code == 200
        assert reloaded.json()["status"] == "succeeded"
    finally:
        process.terminate()
        try:
            output = process.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            process.kill()
            output = process.communicate(timeout=5)[0]
    assert process.returncode in {0, -signal.SIGTERM}, output
    assert _wait_closed(worker_port), output


def _create_proxy_key(control_port: int) -> dict[str, str]:
    response = httpx.post(
        f"http://127.0.0.1:{control_port}/api/admin/proxy-keys",
        headers={"Authorization": "Bearer admin-smoke"},
        json={"name": "runtime-smoke"},
        timeout=5,
        trust_env=False,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _revoke_proxy_key(control_port: int, key_id: str) -> dict[str, str]:
    response = httpx.post(
        f"http://127.0.0.1:{control_port}/api/admin/proxy-keys/{key_id}/revoke",
        headers={"Authorization": "Bearer admin-smoke"},
        timeout=5,
        trust_env=False,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _environment(tmp_path: Path, control_port: int, worker_port: int) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update({
        "QB2API_CONTROL_HOST": "127.0.0.1",
        "QB2API_CONTROL_PORT": str(control_port),
        "QB2API_WORKER_HOST": "127.0.0.1",
        "QB2API_WORKER_PORT": str(worker_port),
        "QB2API_WORKER_AUTOSTART": "1",
        "QB2API_WORKER_START_TIMEOUT_SECONDS": "5",
        "QB2API_ADMIN_UI_ENABLED": "1",
        "QB2API_ADMIN_KEY": "admin-smoke",
        "QB2API_PROXY_API_KEY": "",
        "QB2API_API_KEY": "",
        "QB2API_CREDENTIAL_KEY": Fernet.generate_key().decode(),
        "QB2API_WORKER_INTERNAL_TOKEN": "internal-smoke",
        "QB2API_DATA_DIR": str(tmp_path),
        "QB2API_LOG_DIR": str(tmp_path / "logs"),
        "CODEBUDDY_TOKEN": "ck-smoke",
        "QODER_TOKEN": "",
    })
    return environment


def _wait_json(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + 12
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, headers=headers, timeout=0.5, trust_env=False)
            if response.status_code == 200:
                return response.json()
            last_error = RuntimeError(f"{url} returned {response.status_code}")
        except (httpx.HTTPError, ValueError) as error:
            last_error = error
        time.sleep(0.1)
    raise AssertionError(f"endpoint did not become ready: {url}: {last_error}")


def _wait_closed(port: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as client:
            client.settimeout(0.2)
            if client.connect_ex(("127.0.0.1", port)) != 0:
                return True
        time.sleep(0.1)
    return False


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
