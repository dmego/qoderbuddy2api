"""Worker-owned provider runtime and model routing state."""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

from qb2api.admin.auth import extract_bearer
from qb2api.admin.crypto import hash_token
from qb2api.config import Settings
from qb2api.logger import RequestLogger
from qb2api.models import ModelDefinition
from qb2api.providers import DynamicProviderPool, ProviderRegistry
from qb2api.runtime_snapshot import RuntimeSnapshot

from .control_client import ControlPlaneClient
from .runtime import WorkerRuntime, local_snapshot

logger = logging.getLogger("qb2api.worker.proxy")
SnapshotLoader = Callable[[], Awaitable[RuntimeSnapshot]]


class ProxyState:
    """All proxy-only mutable state for one Worker process."""

    def __init__(
        self,
        settings: Settings,
        snapshot_loader: SnapshotLoader | None = None,
    ) -> None:
        self.settings = settings
        self.runtime: WorkerRuntime | None = None
        self.registry = ProviderRegistry()
        self.request_logger: RequestLogger | None = None
        self.model_definitions: dict[str, list[ModelDefinition]] = {}
        self.model_index: dict[str, set[str]] = {}
        self._snapshot_loader = snapshot_loader

    async def start(self, application: Any) -> None:
        self.request_logger = RequestLogger(
            self.settings.log_dir,
            self.settings.log_requests,
        )
        snapshot = await self._load_snapshot()
        self.runtime = WorkerRuntime(self.settings, self.registry)
        await self.runtime.start(snapshot)
        self.model_definitions = {key: list(value) for key, value in snapshot.models.items()}
        application.state.runtime = self.runtime
        application.state.proxy_state = self
        self._build_model_index()
        logger.info("proxy worker started with providers: %s", self.registry.providers)

    async def close(self) -> None:
        if self.runtime is not None:
            await self.runtime.close()
            self.runtime = None

    async def refresh(self) -> None:
        if self.runtime is None:
            return
        snapshot = await self._load_snapshot()
        await self.runtime.apply(snapshot)
        self.model_definitions = {key: list(value) for key, value in snapshot.models.items()}
        self._build_model_index()

    def verify_proxy_auth(self, authorization: str | None) -> bool:
        if self.runtime is None:
            return False
        if not self.runtime.proxy_auth_required:
            return True
        token = extract_bearer(authorization)
        if token is None:
            return False
        presented = hash_token(token)
        accepted = self.runtime.active_proxy_key_hashes()
        return any(secrets.compare_digest(presented, expected) for expected in accepted)

    async def _load_snapshot(self) -> RuntimeSnapshot:
        if self._snapshot_loader is not None:
            return await self._snapshot_loader()
        if os.getenv("QB2API_WORKER_OWNER_INSTANCE_ID"):
            return await ControlPlaneClient(self.settings).fetch_snapshot()
        return local_snapshot(self.settings)

    def resolve_model(self, model: str) -> tuple[str, str]:
        if "/" in model:
            return self._resolve_prefixed(model)
        return self._resolve_unprefixed(model)

    def available_models(self) -> dict[str, list[ModelDefinition]]:
        result: dict[str, list[ModelDefinition]] = {}
        for name, definitions in self.model_definitions.items():
            if name not in self.registry.providers:
                continue
            provider = self.registry.get(name)
            if isinstance(provider, DynamicProviderPool) and not provider.has_available_slots:
                continue
            result[name] = definitions
        return result

    def _build_model_index(self) -> None:
        self.model_index = {}
        for provider_name in self.registry.providers:
            provider = self.registry.get(provider_name)
            if isinstance(provider, DynamicProviderPool) and not provider.has_available_slots:
                continue
            definitions = self.model_definitions.get(provider_name, [])
            self.model_index[provider_name] = {model.id for model in definitions}

    def _resolve_prefixed(self, model: str) -> tuple[str, str]:
        provider_name, model_id = model.split("/", 1)
        if provider_name not in self.registry.providers:
            raise HTTPException(400, f"Unknown provider: {provider_name}")
        known = self.model_index.get(provider_name)
        if known is not None and model_id not in known:
            raise HTTPException(400, f"Unknown model '{model_id}' for provider '{provider_name}'")
        return provider_name, model_id

    def _resolve_unprefixed(self, model: str) -> tuple[str, str]:
        matches = [name for name, models in self.model_index.items() if model in models]
        if len(matches) == 1:
            return matches[0], model
        if len(matches) > 1:
            raise HTTPException(400, f"Ambiguous model '{model}' found in: {matches}")
        available = [f"{name}/{item}" for name, models in self.model_index.items() for item in sorted(models)]
        raise HTTPException(400, f"Unknown model: {model}. Available: {available[:10]}...")
