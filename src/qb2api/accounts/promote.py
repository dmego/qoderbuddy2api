"""Atomically promote env chat slots into durable accounts."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from .registry import AccountRegistry
from .repository import AccountRepository
from .vault import CredentialVault

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True, slots=True)
class _Promotion:
    account_id: str
    label: str
    payload: dict[str, str]
    mode: str
    chat_capabilities: list[str]
    checkin_status: str
    checkin_capabilities: list[str]
    masked_identity: str


def _durable_id(provider: str) -> str:
    prefix = "cb" if provider == "codebuddy" else "qd"
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


async def promote_env_account(
    registry: AccountRegistry,
    repo: AccountRepository,
    vault: CredentialVault,
    *,
    provider: str,
    account_id: str,
    label: str | None = None,
    durable_id: str | None = None,
) -> str:
    secret = _env_secret(registry, provider, account_id)
    new_id = await _available_id(repo, registry, provider, durable_id)
    promotion = _build_promotion(provider, new_id, label or account_id, secret)
    encrypted = vault.encrypt(promotion.payload)
    async with repo.transaction():
        await _persist_promotion(repo, provider, promotion, encrypted)
    await registry.rebuild()
    return promotion.account_id


def _env_secret(registry: AccountRegistry, provider: str, account_id: str) -> str:
    if provider not in {"codebuddy", "qoder"}:
        raise ValueError(f"unsupported provider: {provider}")
    if not registry.is_env_account(provider, account_id):
        raise LookupError(f"not an env account: {provider}/{account_id}")
    secret = registry.env_secret(provider, account_id, "chat")
    if not secret:
        raise LookupError(f"env secret unavailable or shadowed: {provider}/{account_id}")
    return secret


async def _available_id(
    repo: AccountRepository,
    registry: AccountRegistry,
    provider: str,
    requested: str | None,
) -> str:
    account_id = requested or _durable_id(provider)
    if not _ID_RE.fullmatch(account_id):
        raise ValueError("invalid durable_id")
    if registry.is_env_account(provider, account_id):
        raise ValueError("durable_id collides with env slot")
    existing = {row["account_id"] for row in await repo.list_accounts(provider)}
    if requested and account_id in existing:
        raise ValueError("durable_id already exists")
    while account_id in existing:
        account_id = _durable_id(provider)
    return account_id


def _build_promotion(
    provider: str,
    account_id: str,
    label: str,
    secret: str,
) -> _Promotion:
    is_qoder = provider == "qoder"
    return _Promotion(
        account_id=account_id,
        label=label.strip() or account_id,
        payload={"pat": secret} if is_qoder else {"access_token": secret},
        mode="pat" if is_qoder else "bearer",
        chat_capabilities=["proxy.chat"],
        checkin_status="needs_import" if is_qoder else "unconfigured",
        checkin_capabilities=["checkin.qoder" if is_qoder else "checkin.workbuddy"],
        masked_identity=_mask(secret),
    )


async def _persist_promotion(
    repo: AccountRepository,
    provider: str,
    promotion: _Promotion,
    encrypted_payload: str,
) -> None:
    await repo.upsert_account(
        provider=provider,
        account_id=promotion.account_id,
        label=promotion.label,
        source="promoted",
        enabled=True,
        masked_identity=promotion.masked_identity,
    )
    await _persist_purposes(repo, provider, promotion)
    await repo.upsert_credential(
        provider=provider,
        account_id=promotion.account_id,
        purpose="chat",
        mode=promotion.mode,
        encrypted_payload=encrypted_payload,
    )


async def _persist_purposes(
    repo: AccountRepository,
    provider: str,
    promotion: _Promotion,
) -> None:
    await repo.upsert_purpose(
        provider=provider,
        account_id=promotion.account_id,
        purpose="chat",
        enabled=True,
        status="active",
        verification_status="not_required",
        capabilities=promotion.chat_capabilities,
    )
    await repo.upsert_purpose(
        provider=provider,
        account_id=promotion.account_id,
        purpose="checkin",
        enabled=False,
        status=promotion.checkin_status,
        verification_status="unverified",
        capabilities=promotion.checkin_capabilities,
    )


def _mask(secret: str) -> str:
    return f"{secret[:3]}***{secret[-2:]}" if len(secret) > 8 else "***"


def new_account_slug(provider: str) -> str:
    return _durable_id(provider)
