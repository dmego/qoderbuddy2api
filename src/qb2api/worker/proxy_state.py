"""Worker-owned provider runtime and model routing state."""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from qb2api.admin.auth import extract_bearer
from qb2api.admin.crypto import hash_token
from qb2api.config import Settings
from qb2api.logger import RequestLogger
from qb2api.models import ModelDefinition, load_unified_overrides
from qb2api.models_catalog import UnifiedModel, build_unified_catalog
from qb2api.providers import Provider, ProviderRegistry
from qb2api.providers.qoder_payload import set_runtime_model_keys
from qb2api.runtime_snapshot import RuntimeSnapshot

from .control_client import ControlPlaneClient
from .model_router import ModelRouter
from .runtime import WorkerRuntime, local_snapshot

logger = logging.getLogger("qb2api.worker.proxy")
SnapshotLoader = Callable[[], Awaitable[RuntimeSnapshot]]


@dataclass
class ResolvedModel:
    """Request routing decision for one model id."""

    canonical_id: str
    provider: Provider
    upstream_model: str
    provider_name: str | None = None


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
        self.unified_catalog: dict[str, UnifiedModel] = {}
        self.router: ModelRouter | None = None
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
        self._rebuild_catalog()
        self._sync_runtime_model_keys()
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
        self._rebuild_catalog()
        self._sync_runtime_model_keys()

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

    def resolve_model(self, model: str) -> ResolvedModel:
        if "/" in model:
            return self._resolve_prefixed(model)
        entry = self.unified_catalog.get(model)
        if entry is not None:
            return self._target(entry)
        for candidate in self.unified_catalog.values():
            for route in candidate.routes:
                if route.upstream_id == model:
                    return self._target(candidate)
        raise HTTPException(400, self._unknown_model_message(model))

    def available_models(self) -> list[UnifiedModel]:
        if self.router is not None:
            return self.router.available_models()
        return list(self.unified_catalog.values())

    def _rebuild_catalog(self) -> None:
        overrides = load_unified_overrides(self.settings.model_config_path)
        self.unified_catalog = build_unified_catalog(self.model_definitions, overrides)
        self.router = ModelRouter(self.registry, self.unified_catalog)

    def _target(self, entry: UnifiedModel) -> ResolvedModel:
        if len(entry.routes) == 1:
            route = entry.routes[0]
            provider = self.registry.get(route.provider)
            if provider is None:
                raise HTTPException(400, f"Provider not available: {route.provider}")
            return ResolvedModel(
                canonical_id=entry.id,
                provider=provider,
                upstream_model=route.upstream_id,
                provider_name=route.provider,
            )
        if self.router is None:
            raise HTTPException(503, "model router unavailable")
        return ResolvedModel(
            canonical_id=entry.id,
            provider=self.router,
            upstream_model=entry.id,
        )

    def _resolve_prefixed(self, model: str) -> ResolvedModel:
        provider_name, model_id = model.split("/", 1)
        for candidate in self.unified_catalog.values():
            if candidate.canonicalize(provider_name, model_id) is not None:
                return self._target(candidate)
        raise HTTPException(400, f"Unknown model: {model}")

    def _unknown_model_message(self, model: str) -> str:
        available = [entry.id for entry in sorted(self.unified_catalog.values(), key=lambda e: e.id)]
        return f"Unknown model: {model}. Available: {available[:10]}..."

    def _sync_runtime_model_keys(self) -> None:
        mapping = {
            model.id: model.metadata["cosy_key"]
            for model in self.model_definitions.get("qoder", [])
            if model.metadata and model.metadata.get("cosy_key")
        }
        set_runtime_model_keys(mapping)
