"""Build internal Worker snapshots from Control-owned account state."""

from __future__ import annotations

import logging
from typing import Any

from qb2api.admin.crypto import hash_token
from qb2api.models import load_models_from_config
from qb2api.runtime import RuntimeServices
from qb2api.runtime_snapshot import RuntimeProxyKey, RuntimeSlot, RuntimeSnapshot

logger = logging.getLogger("qb2api.control.snapshot")


class RuntimeSnapshotService:
    """Serialize decrypted credentials only for an authenticated Worker handshake."""

    def __init__(self, runtime: RuntimeServices) -> None:
        self._runtime = runtime
        self._version = 1

    @property
    def version(self) -> int:
        return self._version

    def bump(self) -> None:
        self._version += 1

    async def build(self) -> RuntimeSnapshot:
        slots: list[RuntimeSlot] = []
        registry = self._runtime.account_registry
        resolver = self._runtime.credential_resolver
        if registry is not None and resolver is not None:
            for slot in registry.snapshot("chat"):
                try:
                    credential = await resolver.credential(slot.provider, slot.account_id, "chat")
                except LookupError:
                    logger.warning("snapshot skipped unavailable slot %s/%s", slot.provider, slot.account_id)
                    continue
                token = _primary_token(slot.provider, credential.payload)
                if token:
                    slots.append(RuntimeSlot(slot.provider, slot.account_id, credential.credential_version, token))
        else:
            slots.extend(_env_slots(self._runtime.settings))
        models = load_models_from_config(self._runtime.settings.model_config_path)
        proxy_keys = await self._proxy_keys()
        return RuntimeSnapshot(
            snapshot_version=self._version,
            codebuddy_endpoint=self._runtime.settings.codebuddy_endpoint,
            qoder_timeout=self._runtime.settings.qoder_timeout,
            models={key: tuple(value) for key, value in models.items()},
            slots=tuple(slots),
            proxy_keys=proxy_keys,
        )

    async def _proxy_keys(self) -> tuple[RuntimeProxyKey, ...]:
        keys: list[RuntimeProxyKey] = []
        static_key = self._runtime.settings.proxy_api_key
        if static_key:
            keys.append(RuntimeProxyKey("env", hash_token(static_key)))
        repository = self._runtime.account_repo
        if repository is not None:
            for item in await repository.list_active_proxy_key_hashes():
                keys.append(RuntimeProxyKey(item["key_id"], item["key_hash"]))
        return tuple(keys)


def _primary_token(provider: str, payload: dict[str, Any]) -> str | None:
    if provider == "qoder":
        return payload.get("pat") or payload.get("access_token")
    return payload.get("access_token") or payload.get("token")


def _env_slots(settings: Any) -> list[RuntimeSlot]:
    slots: list[RuntimeSlot] = []
    for index, token in enumerate(settings.codebuddy_tokens or []):
        if token:
            slots.append(RuntimeSlot("codebuddy", f"cb-env-{index}", 1, token))
    for index, token in enumerate(settings.qoder_tokens or []):
        if token:
            slots.append(RuntimeSlot("qoder", f"qd-env-{index}", 1, token))
    return slots
