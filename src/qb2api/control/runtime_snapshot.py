"""Build internal Worker snapshots from Control-owned account state."""

from __future__ import annotations

import logging
from typing import Any

from qb2api.admin.crypto import hash_token
from qb2api.models import ModelCapabilities, ModelDefinition, load_models_from_config
from qb2api.models_catalog import build_unified_catalog
from qb2api.route_policy import RoutePolicy
from qb2api.runtime import RuntimeServices
from qb2api.runtime_snapshot import (
    AccountModelBlock,
    RuntimeProxyKey,
    RuntimeSlot,
    RuntimeSnapshot,
)

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
        upstream = await self._upstream_catalog_models()
        models["qoder"] = upstream
        models["codebuddy"] = await self._filter_catalog_enabled(
            "codebuddy", models.get("codebuddy", [])
        )
        models["workbuddy_intl"] = await self._filter_catalog_enabled(
            "workbuddy_intl", models.get("workbuddy_intl", [])
        )
        models["orcaterm"] = await self._filter_catalog_enabled(
            "orcaterm", models.get("orcaterm", [])
        )
        proxy_keys, proxy_auth_required = await self._proxy_keys()
        return RuntimeSnapshot(
            snapshot_version=self._version,
            codebuddy_endpoint=self._runtime.settings.codebuddy_endpoint,
            workbuddy_intl_endpoint=self._runtime.settings.workbuddy_intl_endpoint,
            qoder_timeout=self._runtime.settings.qoder_timeout,
            models={key: tuple(value) for key, value in models.items()},
            slots=tuple(slots),
            route_policies=await self._route_policies(models),
            account_model_blocks=await self._account_model_blocks(),
            proxy_keys=proxy_keys,
            proxy_auth_required=proxy_auth_required,
        )

    async def _route_policies(
        self,
        models: dict[str, list[ModelDefinition]],
    ) -> dict[str, tuple[RoutePolicy, ...]]:
        """Load stored per-model provider policies for the unified catalog.

        Only policies whose routes actually exist in this catalog generation are
        shipped; a model with no stored policy simply keeps the default
        round-robin ordering in the Worker.
        """
        repository = self._runtime.account_repo
        if repository is None:
            return {}
        from qb2api.models import load_unified_overrides

        overrides = load_unified_overrides(self._runtime.settings.model_config_path)
        routes_by_model = _model_routes(models, overrides)
        policies: dict[str, tuple[RoutePolicy, ...]] = {}
        for row in await repository.list_route_policies():
            model_id = str(row["model_id"])
            providers = routes_by_model.get(model_id)
            if not providers or row["provider"] not in providers:
                continue
            policies.setdefault(model_id, ())
            policies[model_id] = policies[model_id] + (
                RoutePolicy(
                    provider=str(row["provider"]),
                    priority=int(row["priority"]),
                    weight=int(row["weight"]),
                    enabled=bool(row["enabled"]),
                ),
            )
        return policies

    async def _upstream_catalog_models(self) -> list[ModelDefinition]:
        """Provider-catalog models merged into the snapshot with upstream metadata."""
        repository = self._runtime.account_repo
        if repository is None:
            return []
        models: list[ModelDefinition] = []
        for row in await repository.list_models("qoder"):
            model = _to_model_definition(row)
            if model is not None:
                models.append(model)
        return models

    async def _filter_catalog_enabled(
        self,
        provider: str,
        definitions: list[ModelDefinition],
    ) -> list[ModelDefinition]:
        """Drop definitions disabled in the admin catalog; unknown rows stay enabled."""
        repository = self._runtime.account_repo
        if repository is None or not definitions:
            return definitions
        rows = await repository.list_models(provider)
        enabled_by_id = {row["model_id"]: bool(row["enabled"]) for row in rows}
        return [definition for definition in definitions if enabled_by_id.get(definition.id, True)]

    async def _account_model_blocks(self) -> tuple[AccountModelBlock, ...]:
        """Manual per-account exclusions shipped to the Worker."""
        repository = self._runtime.account_repo
        if repository is None:
            return ()
        return tuple(
            AccountModelBlock(
                provider=str(row["provider"]),
                account_id=str(row["account_id"]),
                model_id=str(row["model_id"]),
                reason=str(row.get("reason") or ""),
            )
            for row in await repository.list_account_model_blocks()
            if row.get("source") == "manual"
        )

    async def _proxy_keys(self) -> tuple[tuple[RuntimeProxyKey, ...], bool]:
        keys: list[RuntimeProxyKey] = []
        static_key = self._runtime.settings.proxy_api_key
        if static_key:
            keys.append(RuntimeProxyKey("env", hash_token(static_key)))
        repository = self._runtime.account_repo
        records: list[dict[str, Any]] = []
        if repository is not None:
            records = await repository.list_proxy_key_runtime_records()
            for item in records:
                if item["enabled"] and item["revoked_at"] is None:
                    keys.append(
                        RuntimeProxyKey(
                            item["key_id"],
                            item["key_hash"],
                            item["expires_at"],
                        )
                    )
        return tuple(keys), bool(static_key or records)


def _primary_token(provider: str, payload: dict[str, Any]) -> str | None:
    if provider == "qoder":
        return payload.get("pat") or payload.get("access_token")
    return payload.get("access_token") or payload.get("token")


def _to_model_definition(row: dict[str, Any]) -> ModelDefinition | None:
    """Convert one upstream catalog row into a ModelDefinition, or None when filtered."""
    if row.get("source") != "upstream" or not row.get("enabled"):
        return None
    capabilities = row.get("capabilities") or []
    metadata = row.get("metadata") or {}
    return ModelDefinition(
        id=row["model_id"],
        name=row.get("display_name") or row["model_id"],
        provider="qoder",
        capabilities=ModelCapabilities(
            **{
                name: name in capabilities
                for name in (
                    "chat",
                    "streaming",
                    "tool_calling",
                    "reasoning",
                    "reasoning_effort",
                    "context_window",
                    "max_output_tokens",
                )
            }
        ),
        max_context=int(metadata.get("default_context_window") or 0) or 128000,
        max_output=4096,
        metadata={
            "cosy_key": metadata.get("cosy_key"),
            "default_effort": metadata.get("default_effort"),
        },
    )


def _env_slots(settings: Any) -> list[RuntimeSlot]:
    slots: list[RuntimeSlot] = []
    for index, token in enumerate(settings.codebuddy_tokens or []):
        if token:
            slots.append(RuntimeSlot("codebuddy", f"cb-env-{index}", 1, token))
    for index, token in enumerate(settings.qoder_tokens or []):
        if token:
            slots.append(RuntimeSlot("qoder", f"qd-env-{index}", 1, token))
    for index, token in enumerate(getattr(settings, "workbuddy_intl_tokens", None) or []):
        if token:
            slots.append(RuntimeSlot("workbuddy_intl", f"wbintl-env-{index}", 1, token))
    for index, token in enumerate(getattr(settings, "orcaterm_tokens", None) or []):
        if token:
            slots.append(RuntimeSlot("orcaterm", f"oct-env-{index}", 1, token))
    return slots


def _model_routes(
    models: dict[str, list[ModelDefinition]],
    overrides: dict[str, Any] | None = None,
) -> dict[str, set[str]]:
    """Map every unified model id to the providers that can serve it."""
    catalog = build_unified_catalog(models, overrides)
    return {
        model_id: {route.provider for route in entry.routes}
        for model_id, entry in catalog.items()
    }
