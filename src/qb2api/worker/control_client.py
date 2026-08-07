"""Authenticated loopback client for the Control Plane runtime snapshot."""

from __future__ import annotations

import asyncio
import os
import time

import httpx

from qb2api.config import Settings
from qb2api.runtime_snapshot import RUNTIME_PROTOCOL_VERSION, RuntimeSnapshot


class ControlPlaneClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def fetch_snapshot(self) -> RuntimeSnapshot:
        owner = os.getenv("QB2API_WORKER_OWNER_INSTANCE_ID", "")
        auth_version = _auth_version()
        token = self._settings.worker_internal_token or ""
        if not owner or not auth_version or not token:
            raise RuntimeError("worker control identity is incomplete")
        payload = {
            "protocol_version": RUNTIME_PROTOCOL_VERSION,
            "owner_instance_id": owner,
            "internal_auth_version": auth_version,
        }
        deadline = time.monotonic() + max(5, self._settings.worker_start_timeout_seconds)
        async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
            while time.monotonic() < deadline:
                try:
                    response = await client.post(
                        f"{_control_url(self._settings)}/api/control/worker/handshake",
                        headers={"X-QB2API-Worker-Token": token},
                        json=payload,
                    )
                except httpx.HTTPError:
                    await asyncio.sleep(0.1)
                    continue
                if response.status_code == 200:
                    return _decode_snapshot(response)
                if response.status_code in {401, 409}:
                    raise RuntimeError(
                        f"control snapshot rejected with status {response.status_code}"
                    )
                await asyncio.sleep(0.1)
        raise RuntimeError("control snapshot request timed out")


def _control_url(settings: Settings) -> str:
    host = settings.control_host
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    return f"http://{host}:{settings.control_port}"


def _auth_version() -> int:
    try:
        return int(os.getenv("QB2API_WORKER_INTERNAL_AUTH_VERSION", "0"))
    except ValueError:
        return 0


def _decode_snapshot(response: httpx.Response) -> RuntimeSnapshot:
    try:
        return RuntimeSnapshot.from_payload(response.json())
    except (TypeError, ValueError) as error:
        raise RuntimeError("control returned an invalid runtime snapshot") from error
