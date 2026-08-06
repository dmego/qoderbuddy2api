"""Reusable test doubles and setup helpers for check-in service contracts."""

from __future__ import annotations

import asyncio

from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.accounts.vault import CredentialVault
from qb2api.checkin.models import CheckInOutcome, CheckInResult
from qb2api.checkin.service import CheckinService
from qb2api.config import Settings


class SequenceClient:
    def __init__(self, provider: str, results: list[CheckInResult | BaseException]) -> None:
        self.provider = provider
        self.results = results
        self.calls = 0

    async def checkin(self, **_values) -> CheckInResult:
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def refresh(self, **_values):
        raise AssertionError("refresh not expected")

    async def close(self) -> None:
        return None


class BlockingClient(SequenceClient):
    def __init__(self) -> None:
        super().__init__("codebuddy", [])
        self.started = asyncio.Event()

    async def checkin(self, **_values) -> CheckInResult:
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def seed(
    repository: AccountRepository,
    vault: CredentialVault,
    provider: str,
    account_id: str,
) -> None:
    payload = {"access_token": f"access-{account_id}"}
    if provider == "qoder":
        payload["refresh_token"] = f"refresh-{account_id}"
    async with repository.transaction():
        await repository.upsert_account(
            provider=provider, account_id=account_id, label=account_id, source="manual", enabled=True,
        )
        await repository.upsert_purpose(
            provider=provider, account_id=account_id, purpose="chat", enabled=True, status="active",
            verification_status="not_required", capabilities=["proxy.chat"],
        )
        await repository.upsert_purpose(
            provider=provider, account_id=account_id, purpose="checkin", enabled=True, status="active",
            verification_status="verified", capabilities=[f"checkin.{provider}"],
        )
        await repository.upsert_credential(
            provider=provider, account_id=account_id, purpose="checkin",
            mode="access_refresh" if provider == "qoder" else "bearer",
            encrypted_payload=vault.encrypt(payload), has_refresh_token=provider == "qoder",
        )


async def registry(repository: AccountRepository, vault: CredentialVault) -> AccountRegistry:
    value = AccountRegistry(repository, vault)
    await value.rebuild()
    return value


def service(
    repository: AccountRepository,
    vault: CredentialVault,
    account_registry: AccountRegistry,
    *,
    workbuddy,
    qoder,
    codebuddy_enabled: bool,
    qoder_enabled: bool,
    retry_limit: int = 0,
    growth_automation=None,
) -> CheckinService:
    settings = Settings(
        checkin_enabled=True,
        codebuddy_checkin_enabled=codebuddy_enabled,
        qoder_checkin_enabled=qoder_enabled,
        checkin_retry_limit=retry_limit,
        codebuddy_tokens=[],
        qoder_tokens=[],
    )
    return CheckinService(
        settings=settings,
        repo=repository,
        registry=account_registry,
        resolver=CredentialResolver(repository, vault, account_registry),
        vault=vault,
        workbuddy=workbuddy,
        qoder=qoder,
        growth_automation=growth_automation,
    )


def success(provider: str, account_id: str) -> CheckInResult:
    return CheckInResult(
        outcome=CheckInOutcome.CLAIMED, provider=provider, account_id=account_id, http_status=200,
    )
