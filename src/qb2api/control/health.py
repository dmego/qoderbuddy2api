"""Authenticated Worker readiness probe."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from qb2api.config import Settings

from .service_models import WorkerIdentity


class WorkerHealthChecker:
    def __init__(
        self,
        settings: Settings,
        identity: Callable[[], WorkerIdentity | None],
    ) -> None:
        self._settings = settings
        self._identity = identity

    async def __call__(self, process: Any) -> bool:
        if process is None or process.poll() is not None:
            return False
        url = f"http://{self._settings.worker_host}:{self._settings.worker_port}/internal/health/ready"
        try:
            async with httpx.AsyncClient(timeout=0.5) as client:
                response = await client.get(url, headers=self._headers())
            return self._matches(response)
        except (httpx.HTTPError, ValueError):
            return False

    async def reload(self, process: Any) -> int:
        if process is None or process.poll() is not None:
            raise RuntimeError("worker is not running")
        url = f"http://{self._settings.worker_host}:{self._settings.worker_port}/internal/runtime/reload"
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
                response = await client.post(url, headers=self._headers())
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RuntimeError("worker reload request failed") from error
        if response.status_code != 200 or payload.get("status") != "reloaded":
            raise RuntimeError("worker rejected runtime reload")
        if not self._identity_payload_matches(payload):
            raise RuntimeError("worker identity changed during reload")
        version = payload.get("snapshot_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise RuntimeError("worker returned an invalid snapshot version")
        return version

    def _matches(self, response: httpx.Response) -> bool:
        payload = response.json()
        return (
            response.status_code == 200
            and payload.get("status") == "ready"
            and self._identity_payload_matches(payload)
        )

    def _identity_payload_matches(self, payload: dict[str, Any]) -> bool:
        identity = self._identity()
        if identity is None:
            return False
        try:
            auth_version = int(payload.get("internal_auth_version", -1))
        except (TypeError, ValueError):
            return False
        return (
            payload.get("owner_instance_id") == identity.owner_instance_id
            and auth_version == identity.internal_auth_version
        )

    def _headers(self) -> dict[str, str]:
        if not self._settings.worker_internal_token:
            return {}
        return {"X-QB2API-Worker-Token": self._settings.worker_internal_token}
