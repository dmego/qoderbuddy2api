"""Memory-only provider runtime assembled from a Control Plane snapshot."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime

from qb2api.config import Settings
from qb2api.models import load_models_from_config
from qb2api.provider_factory import ProviderFactory
from qb2api.providers import DynamicProviderPool, Provider, ProviderRegistry
from qb2api.route_policy import RoutePolicy
from qb2api.runtime_snapshot import RuntimeProxyKey, RuntimeSlot, RuntimeSnapshot


class WorkerRuntime:
    """Own stable provider pools without storage or credential decryption."""

    def __init__(self, settings: Settings, providers: ProviderRegistry) -> None:
        self.settings = settings
        self.providers = providers
        self.codebuddy_pool = DynamicProviderPool("codebuddy")
        self.qoder_pool = DynamicProviderPool("qoder")
        self.workbuddy_intl_pool = DynamicProviderPool("workbuddy_intl")
        self.orcaterm_pool = DynamicProviderPool("orcaterm")
        self.snapshot_version = 0
        self.proxy_key_hashes: frozenset[str] = frozenset()
        self.proxy_key_expirations: tuple[tuple[str, float | None], ...] = ()
        self.proxy_auth_required = False
        self.route_policies: dict[str, tuple[RoutePolicy, ...]] = {}
        self._slot_providers: dict[str, Provider] = {}
        self._slot_signatures: dict[str, str] = {}

    async def start(self, snapshot: RuntimeSnapshot) -> None:
        self.providers.clear()
        self.providers.register(self.codebuddy_pool)
        self.providers.register(self.qoder_pool)
        self.providers.register(self.workbuddy_intl_pool)
        self.providers.register(self.orcaterm_pool)
        await self.apply(snapshot)

    async def apply(self, snapshot: RuntimeSnapshot) -> None:
        if snapshot.snapshot_version < self.snapshot_version:
            raise ValueError("stale runtime snapshot")
        runtime_settings = replace(
            self.settings,
            codebuddy_endpoint=snapshot.codebuddy_endpoint,
            workbuddy_intl_endpoint=snapshot.workbuddy_intl_endpoint,
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
        self.route_policies = dict(snapshot.route_policies)
        self._apply_account_model_blocks(snapshot)
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

    def _apply_account_model_blocks(self, snapshot: RuntimeSnapshot) -> None:
        """Push manual (provider, account, model) exclusions into each pool."""
        per_pool: dict[str, dict[tuple[str, str], str]] = {}
        for block in snapshot.account_model_blocks:
            key = (f"{block.provider}:{block.account_id}", block.model_id)
            per_pool.setdefault(block.provider, {})[key] = block.reason
        for name, pool in (
            ("codebuddy", self.codebuddy_pool),
            ("qoder", self.qoder_pool),
            ("workbuddy_intl", self.workbuddy_intl_pool),
            ("orcaterm", self.orcaterm_pool),
        ):
            pool.set_hard_blocks(per_pool.get(name, {}))

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
        pools = {
            "codebuddy": self.codebuddy_pool,
            "qoder": self.qoder_pool,
            "workbuddy_intl": self.workbuddy_intl_pool,
            "orcaterm": self.orcaterm_pool,
        }
        for name, pool in pools.items():
            prefix = f"{name}:"
            await pool.update_slots({
                key: value for key, value in providers.items() if key.startswith(prefix)
            })


_ENV_TOKEN_SLOTS = (
    ("codebuddy", "cb-env", "codebuddy_tokens"),
    ("qoder", "qd-env", "qoder_tokens"),
    ("workbuddy_intl", "wbintl-env", "workbuddy_intl_tokens"),
    ("orcaterm", "oct-env", "orcaterm_tokens"),
)


def _env_slots(settings: Settings) -> list[RuntimeSlot]:
    slots: list[RuntimeSlot] = []
    for provider, prefix, attribute in _ENV_TOKEN_SLOTS:
        for index, token in enumerate(getattr(settings, attribute, None) or []):
            if token:
                slots.append(RuntimeSlot(provider, f"{prefix}-{index}", 1, token))
    return slots


def local_snapshot(settings: Settings) -> RuntimeSnapshot:
    """Build the storage-free legacy Worker snapshot from environment tokens."""
    slots = _env_slots(settings)
    models = load_models_from_config(settings.model_config_path)
    proxy_keys = ()
    if settings.proxy_api_key:
        proxy_keys = (RuntimeProxyKey("env", hashlib.sha256(settings.proxy_api_key.encode()).hexdigest()),)
    return RuntimeSnapshot(
        snapshot_version=1,
        codebuddy_endpoint=settings.codebuddy_endpoint,
        workbuddy_intl_endpoint=settings.workbuddy_intl_endpoint,
        qoder_timeout=settings.qoder_timeout,
        models={key: tuple(value) for key, value in models.items()},
        slots=tuple(slots),
        proxy_keys=proxy_keys,
        proxy_auth_required=bool(settings.proxy_api_key),
    )


def _slot_identity(slot: RuntimeSlot) -> tuple[str, str]:
    if slot.provider not in {"codebuddy", "qoder", "workbuddy_intl", "orcaterm"}:
        raise ValueError(f"unsupported runtime provider: {slot.provider}")
    digest = hashlib.sha256(slot.token.encode()).hexdigest()
    return f"{slot.provider}:{slot.account_id}", f"v{slot.credential_version}:{digest}"


def _build_provider(factory: ProviderFactory, slot: RuntimeSlot) -> Provider:
    if slot.provider == "codebuddy":
        return factory.codebuddy_static(slot.token)
    if slot.provider == "workbuddy_intl":
        return factory.workbuddy_intl_static(slot.token)
    if slot.provider == "orcaterm":
        return factory.orcaterm_static(slot.token)
    return factory.qoder(slot.token)


def _expiry_timestamp(value: str | None) -> float | None:
    if value is None:
        return None
    return datetime.fromisoformat(value).timestamp()
