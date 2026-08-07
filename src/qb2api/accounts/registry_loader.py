"""Build secret-safe registry slots from environment and durable storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .repository import AccountRepository
from .vault import CredentialVault


@dataclass
class EnvSlot:
    provider: str
    account_id: str
    secret: str
    index: int
    shadowed: bool = False


@dataclass
class DynamicSlot:
    provider: str
    account_id: str
    label: str
    source: str
    enabled: bool
    masked_identity: str | None
    created_at: str | None
    updated_at: str | None
    purposes: dict[str, dict[str, Any]] = field(default_factory=dict)
    chat_secret: str | None = None


def load_environment(
    *,
    codebuddy_tokens: list[str],
    qoder_tokens: list[str],
) -> tuple[list[EnvSlot], dict[tuple[str, str, str], str]]:
    slots = _environment_slots("codebuddy", "cb-env", codebuddy_tokens)
    slots.extend(_environment_slots("qoder", "qd-env", qoder_tokens))
    secrets = {(slot.provider, slot.account_id, "chat"): slot.secret for slot in slots}
    return slots, secrets


def _environment_slots(provider: str, prefix: str, tokens: list[str]) -> list[EnvSlot]:
    return [
        EnvSlot(provider, f"{prefix}-{index}", token, index)
        for index, token in enumerate(tokens)
        if token
    ]


async def load_dynamic_slots(
    repo: AccountRepository,
    vault: CredentialVault | None,
) -> dict[tuple[str, str], DynamicSlot]:
    slots: dict[tuple[str, str], DynamicSlot] = {}
    for account in await repo.list_accounts():
        slot = await _load_dynamic_slot(repo, vault, account)
        slots[(slot.provider, slot.account_id)] = slot
    return slots


async def _load_dynamic_slot(
    repo: AccountRepository,
    vault: CredentialVault | None,
    account: dict[str, Any],
) -> DynamicSlot:
    provider = account["provider"]
    account_id = account["account_id"]
    purposes = _purpose_map(await repo.list_purposes(provider, account_id))
    chat_secret = await _load_chat_secret(
        repo,
        vault,
        account=account,
        purposes=purposes,
    )
    return DynamicSlot(
        provider=provider,
        account_id=account_id,
        label=account.get("label") or account_id,
        source=account.get("source") or "manual",
        enabled=bool(account.get("enabled")),
        masked_identity=account.get("masked_identity"),
        created_at=account.get("created_at"),
        updated_at=account.get("updated_at"),
        purposes=purposes,
        chat_secret=chat_secret,
    )


def _purpose_map(purposes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    fields = (
        "enabled",
        "status",
        "verification_status",
        "verified_at",
        "expires_at",
        "last_success_at",
        "failure_count",
        "last_error",
    )
    return {
        purpose["purpose"]: {
            **{field: purpose.get(field) for field in fields},
            "capabilities": list(purpose.get("capabilities") or []),
            "failure_count": purpose.get("failure_count", 0),
        }
        for purpose in purposes
    }


async def _load_chat_secret(
    repo: AccountRepository,
    vault: CredentialVault | None,
    *,
    account: dict[str, Any],
    purposes: dict[str, dict[str, Any]],
) -> str | None:
    if vault is None or not account.get("enabled") or not purposes.get("chat", {}).get("enabled"):
        return None
    credential = await repo.get_credential(account["provider"], account["account_id"], "chat")
    if credential is None:
        return None
    try:
        payload = vault.decrypt(credential["encrypted_payload"])
    except ValueError:
        return None
    return primary_secret(account["provider"], payload)


def primary_secret(provider: str, payload: dict[str, Any]) -> str | None:
    if provider == "qoder":
        return payload.get("pat") or payload.get("access_token")
    return payload.get("access_token") or payload.get("token")


def mark_shadowed(environment: list[EnvSlot], dynamic: dict[tuple[str, str], DynamicSlot]) -> None:
    for env_slot in environment:
        env_slot.shadowed = any(
            dynamic_slot.enabled
            and dynamic_slot.provider == env_slot.provider
            and dynamic_slot.chat_secret
            and _secrets_equal(dynamic_slot.chat_secret, env_slot.secret)
            for dynamic_slot in dynamic.values()
        )


def _secrets_equal(left: str, right: str) -> bool:
    import hmac

    left_bytes, right_bytes = left.encode(), right.encode()
    if len(left_bytes) != len(right_bytes):
        return hmac.compare_digest(left_bytes, left_bytes) and False
    return hmac.compare_digest(left_bytes, right_bytes)
