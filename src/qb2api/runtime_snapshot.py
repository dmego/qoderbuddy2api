"""Versioned, internal-only runtime data exchanged by Control and Worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .models import ModelCapabilities, ModelDefinition
from .route_policy import RoutePolicy

# v3 adds per-model route policies (priority/weight/enabled per provider).
RUNTIME_PROTOCOL_VERSION = 3


@dataclass(frozen=True, slots=True)
class RuntimeSlot:
    """One in-memory provider credential; never expose this object to UI code."""

    provider: str
    account_id: str
    credential_version: int
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class RuntimeProxyKey:
    """A one-way key digest accepted by the Worker proxy boundary."""

    key_id: str
    key_hash: str = field(repr=False)
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Complete Worker input for a single immutable runtime generation."""

    snapshot_version: int
    codebuddy_endpoint: str
    qoder_timeout: int
    models: dict[str, tuple[ModelDefinition, ...]]
    slots: tuple[RuntimeSlot, ...]
    proxy_keys: tuple[RuntimeProxyKey, ...] = ()
    proxy_auth_required: bool = False
    protocol_version: int = RUNTIME_PROTOCOL_VERSION
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    workbuddy_intl_endpoint: str = "https://www.workbuddy.ai"
    route_policies: dict[str, tuple[RoutePolicy, ...]] = field(default_factory=dict)
    account_model_blocks: tuple[AccountModelBlock, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "snapshot_version": self.snapshot_version,
            "generated_at": self.generated_at,
            "codebuddy_endpoint": self.codebuddy_endpoint,
            "workbuddy_intl_endpoint": self.workbuddy_intl_endpoint,
            "qoder_timeout": self.qoder_timeout,
            "models": {
                provider: [_model_payload(model) for model in values]
                for provider, values in self.models.items()
            },
            "slots": [
                {
                    "provider": slot.provider,
                    "account_id": slot.account_id,
                    "credential_version": slot.credential_version,
                    "token": slot.token,
                }
                for slot in self.slots
            ],
            "route_policies": {
                model_id: [policy.to_payload() for policy in policies]
                for model_id, policies in self.route_policies.items()
            },
            "account_model_blocks": [
                block.to_payload() for block in self.account_model_blocks
            ],
            "proxy_keys": [
                {
                    "key_id": key.key_id,
                    "key_hash": key.key_hash,
                    "expires_at": key.expires_at,
                }
                for key in self.proxy_keys
            ],
            "proxy_auth_required": self.proxy_auth_required,
        }

    @classmethod
    def from_payload(cls, value: Any) -> RuntimeSnapshot:
        if not isinstance(value, dict):
            raise ValueError("runtime snapshot must be an object")
        protocol = _positive_int(value.get("protocol_version"), "protocol_version")
        if protocol != RUNTIME_PROTOCOL_VERSION:
            raise ValueError(f"unsupported runtime protocol: {protocol}")
        models = _parse_models(value.get("models"))
        slots = _parse_slots(value.get("slots"))
        proxy_keys = _parse_proxy_keys(value.get("proxy_keys", []))
        return cls(
            snapshot_version=_positive_int(value.get("snapshot_version"), "snapshot_version"),
            generated_at=_text(value.get("generated_at"), "generated_at"),
            codebuddy_endpoint=_text(value.get("codebuddy_endpoint"), "codebuddy_endpoint"),
            workbuddy_intl_endpoint=_text(
                value.get("workbuddy_intl_endpoint") or "https://www.workbuddy.ai",
                "workbuddy_intl_endpoint",
            ),
            qoder_timeout=_positive_int(value.get("qoder_timeout"), "qoder_timeout"),
            models=models,
            slots=slots,
            proxy_keys=proxy_keys,
            route_policies=_parse_route_policies(value.get("route_policies", {})),
            account_model_blocks=_parse_account_model_blocks(
                value.get("account_model_blocks", [])
            ),
            proxy_auth_required=_boolean(
                value.get("proxy_auth_required"), "proxy_auth_required"
            ),
            protocol_version=protocol,
        )


@dataclass(frozen=True, slots=True)
class AccountModelBlock:
    """One administrator-declared (provider, account, model) exclusion."""

    provider: str
    account_id: str
    model_id: str
    reason: str = ""

    def to_payload(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "account_id": self.account_id,
            "model_id": self.model_id,
            "reason": self.reason,
        }


def _parse_account_model_blocks(value: Any) -> tuple[AccountModelBlock, ...]:
    if not isinstance(value, list) or len(value) > 5000:
        raise ValueError("invalid runtime account model blocks")
    blocks: list[AccountModelBlock] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid runtime account model block")
        blocks.append(AccountModelBlock(
            provider=_text(item.get("provider"), "block.provider"),
            account_id=_text(item.get("account_id"), "block.account_id"),
            model_id=_text(item.get("model_id"), "block.model_id"),
            reason=str(item.get("reason") or "")[:200],
        ))
    return tuple(blocks)


def _parse_route_policies(value: Any) -> dict[str, tuple[RoutePolicy, ...]]:
    if not isinstance(value, dict) or len(value) > 2000:
        raise ValueError("invalid runtime route policies")
    parsed: dict[str, tuple[RoutePolicy, ...]] = {}
    for model_id, entries in value.items():
        if not isinstance(model_id, str) or not model_id or len(model_id) > 256:
            raise ValueError("invalid route policy model id")
        if not isinstance(entries, list) or len(entries) > 20:
            raise ValueError("invalid route policy list")
        parsed[model_id] = tuple(RoutePolicy.from_payload(item) for item in entries)
    return parsed


def _model_payload(model: ModelDefinition) -> dict[str, Any]:
    payload = {
        "id": model.id,
        "name": model.name,
        "provider": model.provider,
        "max_context": model.max_context,
        "max_output": model.max_output,
        "capabilities": {
            "chat": model.capabilities.chat,
            "streaming": model.capabilities.streaming,
            "tool_calling": model.capabilities.tool_calling,
            "reasoning": model.capabilities.reasoning,
            "reasoning_effort": model.capabilities.reasoning_effort,
            "context_window": model.capabilities.context_window,
            "max_output_tokens": model.capabilities.max_output_tokens,
        },
    }
    if model.metadata:
        payload["metadata"] = model.metadata
    return payload


def _parse_models(value: Any) -> dict[str, tuple[ModelDefinition, ...]]:
    if not isinstance(value, dict) or len(value) > 20:
        raise ValueError("invalid runtime models")
    parsed: dict[str, tuple[ModelDefinition, ...]] = {}
    for provider, entries in value.items():
        if not isinstance(provider, str) or not isinstance(entries, list) or len(entries) > 500:
            raise ValueError("invalid runtime model list")
        parsed[provider] = tuple(_parse_model(provider, item) for item in entries)
    return parsed


def _parse_model(provider: str, value: Any) -> ModelDefinition:
    if not isinstance(value, dict):
        raise ValueError("invalid runtime model")
    capabilities = value.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ValueError("invalid runtime model capabilities")
    fields = {name: bool(capabilities.get(name, False)) for name in (
        "chat", "streaming", "tool_calling", "reasoning", "reasoning_effort", "context_window", "max_output_tokens"
    )}
    raw_metadata = value.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else None
    return ModelDefinition(
        id=_text(value.get("id"), "model.id"),
        name=_text(value.get("name"), "model.name"),
        provider=provider,
        capabilities=ModelCapabilities(**fields),
        max_context=_positive_int(value.get("max_context"), "model.max_context"),
        max_output=_positive_int(value.get("max_output"), "model.max_output"),
        metadata=metadata,
    )


def _parse_slots(value: Any) -> tuple[RuntimeSlot, ...]:
    if not isinstance(value, list) or len(value) > 2000:
        raise ValueError("invalid runtime slots")
    slots: list[RuntimeSlot] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid runtime slot")
        slots.append(RuntimeSlot(
            provider=_text(item.get("provider"), "slot.provider"),
            account_id=_text(item.get("account_id"), "slot.account_id"),
            credential_version=_positive_int(item.get("credential_version"), "slot.credential_version"),
            token=_text(item.get("token"), "slot.token"),
        ))
    return tuple(slots)


def _parse_proxy_keys(value: Any) -> tuple[RuntimeProxyKey, ...]:
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError("invalid runtime proxy keys")
    keys: list[RuntimeProxyKey] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid runtime proxy key")
        key_hash = _text(item.get("key_hash"), "proxy_key.key_hash")
        if len(key_hash) != 64 or any(char not in "0123456789abcdef" for char in key_hash):
            raise ValueError("invalid proxy key hash")
        keys.append(RuntimeProxyKey(
            key_id=_text(item.get("key_id"), "proxy_key.key_id"),
            key_hash=key_hash,
            expires_at=_optional_timestamp(item.get("expires_at")),
        ))
    return tuple(keys)


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError(f"invalid {field_name}")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"invalid {field_name}")
    return value


def _boolean(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"invalid {field_name}")
    return value


def _optional_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid proxy_key.expires_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid proxy_key.expires_at") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()
