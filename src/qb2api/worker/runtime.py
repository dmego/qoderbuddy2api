"""Memory-only provider runtime assembled from a Control Plane snapshot."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

from qb2api.config import Settings
from qb2api.models import load_models_from_config
from qb2api.provider_factory import ProviderFactory
from qb2api.providers import DynamicProviderPool, Provider, ProviderRegistry
from qb2api.runtime_snapshot import RuntimeProxyKey, RuntimeSlot, RuntimeSnapshot


class WorkerRuntime:
    """Own stable provider pools without storage or credential decryption."""

    def __init__(self, settings: Settings, providers: ProviderRegistry) -> None:
        self.settings = settings
        self.providers = providers
        self.codebuddy_pool = DynamicProviderPool("codebuddy")
        self.qoder_pool = DynamicProviderPool("qoder")
        self.snapshot_version = 0
        self.proxy_key_hashes: frozenset[str] = frozenset()
        self.proxy_key_expirations: tuple[tuple[str, float | None], ...] = ()
        self.proxy_auth_required = False
        self._slot_providers: dict[str, Provider] = {}
        self._slot_signatures: dict[str, str] = {}

    async def start(self, snapshot: RuntimeSnapshot) -> None:
        self.providers.clear()
        self.providers.register(self.codebuddy_pool)
        self.providers.register(self.qoder_pool)
        await self.apply(snapshot)

    async def apply(self, snapshot: RuntimeSnapshot) -> None:
        if snapshot.snapshot_version < self.snapshot_version:
            raise ValueError("stale runtime snapshot")
        runtime_settings = replace(
            self.settings,
            codebuddy_endpoint=snapshot.codebuddy_endpoint,
            qoder_timeout=snapshot.qoder_timeout,
        )
        factory = ProviderFactory(runtime_settings)
        candidates: dict[str, Provider] = {}
        signatures: dict[str, str] = {}
        for slot in snapshot.slots:
            key, signature = _slot_identity(slot)
            provider = self._reuse(key, signature)
            candidates[key] = provider or _build_provider(factory, slot)
            signatures[key] = signature
        await self._publish(candidates)
        self._slot_providers = candidates
        self._slot_signatures = signatures
        self.snapshot_version = snapshot.snapshot_version
        self.proxy_key_hashes = frozenset(key.key_hash for key in snapshot.proxy_keys)
        self.proxy_key_expirations = tuple(
            (key.key_hash, _expiry_timestamp(key.expires_at))
            for key in snapshot.proxy_keys
        )
        self.proxy_auth_required = bool(
            snapshot.proxy_auth_required or snapshot.proxy_keys
        )

    async def close(self) -> None:
        await self.providers.close_all()
        self._slot_providers = {}
        self._slot_signatures = {}

    def active_proxy_key_hashes(self) -> tuple[str, ...]:
        now = datetime.now(UTC).timestamp()
        return tuple(
            key_hash
            for key_hash, expires_at in self.proxy_key_expirations
            if expires_at is None or expires_at > now
        )

    def _reuse(self, key: str, signature: str) -> Provider | None:
        if self._slot_signatures.get(key) != signature:
            return None
        return self._slot_providers.get(key)

    async def _publish(self, providers: dict[str, Provider]) -> None:
        codebuddy = {key: value for key, value in providers.items() if key.startswith("codebuddy:")}
        qoder = {key: value for key, value in providers.items() if key.startswith("qoder:")}
        await self.codebuddy_pool.update_slots(codebuddy)
        await self.qoder_pool.update_slots(qoder)


def local_snapshot(settings: Settings) -> RuntimeSnapshot:
    """Build the storage-free legacy Worker snapshot from environment tokens."""
    slots = [
        RuntimeSlot("codebuddy", f"cb-env-{index}", 1, token)
        for index, token in enumerate(settings.codebuddy_tokens or [])
        if token
    ]
    slots.extend(
        RuntimeSlot("qoder", f"qd-env-{index}", 1, token)
        for index, token in enumerate(settings.qoder_tokens or [])
        if token
    )
    models = load_models_from_config(settings.model_config_path)
    proxy_keys = ()
    if settings.proxy_api_key:
        proxy_keys = (RuntimeProxyKey("env", hashlib.sha256(settings.proxy_api_key.encode()).hexdigest()),)
    return RuntimeSnapshot(
        snapshot_version=1,
        codebuddy_endpoint=settings.codebuddy_endpoint,
        qoder_timeout=settings.qoder_timeout,
        models={key: tuple(value) for key, value in models.items()},
        slots=tuple(slots),
        proxy_keys=proxy_keys,
        proxy_auth_required=bool(settings.proxy_api_key),
    )


def _slot_identity(slot: RuntimeSlot) -> tuple[str, str]:
    if slot.provider not in {"codebuddy", "qoder"}:
        raise ValueError(f"unsupported runtime provider: {slot.provider}")
    digest = hashlib.sha256(slot.token.encode()).hexdigest()
    return f"{slot.provider}:{slot.account_id}", f"v{slot.credential_version}:{digest}"


def _build_provider(factory: ProviderFactory, slot: RuntimeSlot) -> Provider:
    if slot.provider == "codebuddy":
        return factory.codebuddy_static(slot.token)
    return factory.qoder(slot.token)


def _expiry_timestamp(value: str | None) -> float | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).timestamp()
