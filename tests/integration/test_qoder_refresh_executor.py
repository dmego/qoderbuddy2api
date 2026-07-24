"""Qoder refresh classification and credential CAS integration contracts."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository, CredentialVersionConflict
from qb2api.accounts.resolver import CredentialResolver
from qb2api.accounts.vault import CredentialVault
from qb2api.checkin.executors import CheckinExecutor
from qb2api.checkin.models import CheckInOutcome, CheckInResult, RefreshResult
from qb2api.config import Settings


class _UnusedWorkBuddy:
    async def close(self) -> None:
        return None


class _RefreshingQoderClient:
    def __init__(
        self,
        *,
        refresh: RefreshResult,
        success_token: str | None = None,
    ) -> None:
        self.refresh_result = refresh
        self.success_token = success_token
        self.access_tokens: list[str] = []

    async def checkin(self, *, access_token: str, account_id: str) -> CheckInResult:
        self.access_tokens.append(access_token)
        if self.success_token and access_token == self.success_token:
            return CheckInResult(
                outcome=CheckInOutcome.CLAIMED,
                provider="qoder",
                account_id=account_id,
                http_status=200,
            )
        return CheckInResult(
            outcome=CheckInOutcome.NEEDS_REAUTH,
            provider="qoder",
            account_id=account_id,
            http_status=401,
        )

    async def refresh(self, **_values) -> RefreshResult:
        return self.refresh_result

    async def close(self) -> None:
        return None


@pytest.fixture
async def qoder_context(tmp_path):
    repository = AccountRepository(str(tmp_path / "accounts.sqlite3"))
    await repository.connect()
    await repository.migrate()
    vault = CredentialVault(Fernet.generate_key().decode())
    await _seed(repository, vault)
    registry = AccountRegistry(repository, vault)
    await registry.rebuild()
    yield repository, vault, registry
    await repository.close()


@pytest.mark.asyncio
async def test_rate_limit_does_not_mark_purpose_needs_reauth(qoder_context) -> None:
    repository, vault, registry = qoder_context
    qoder = _RefreshingQoderClient(
        refresh=RefreshResult(
            http_status=429,
            outcome=CheckInOutcome.RATE_LIMITED,
            message="rate limited",
        )
    )
    executor = _executor(repository, vault, registry, qoder=qoder)

    result = await executor.run("qoder", "qd-main")
    purposes = await repository.list_purposes("qoder", "qd-main")
    checkin = next(item for item in purposes if item["purpose"] == "checkin")

    assert result.outcome == CheckInOutcome.RATE_LIMITED
    assert checkin["status"] == "active"
    await executor.close()


@pytest.mark.asyncio
async def test_cas_conflict_uses_winning_credential(qoder_context) -> None:
    repository, vault, registry = qoder_context
    qoder = _RefreshingQoderClient(
        refresh=RefreshResult(
            access_token="stale-refresh-result",
            refresh_token="stale-rotated-refresh",
        ),
        success_token="winner-access",
    )
    original_upsert = repository.upsert_credential

    async def competing_upsert(**values):
        if values.get("expected_version") is not None:
            await original_upsert(
                provider="qoder",
                account_id="qd-main",
                purpose="checkin",
                mode="access_refresh",
                encrypted_payload=vault.encrypt(
                    {
                        "access_token": "winner-access",
                        "refresh_token": "winner-refresh",
                    }
                ),
                has_refresh_token=True,
                expected_version=values["expected_version"],
            )
            raise CredentialVersionConflict("injected competing refresh")
        return await original_upsert(**values)

    repository.upsert_credential = competing_upsert  # type: ignore[method-assign]
    executor = _executor(repository, vault, registry, qoder=qoder)

    result = await executor.run("qoder", "qd-main")
    stored = await repository.get_credential("qoder", "qd-main", "checkin")
    assert stored is not None
    payload = vault.decrypt(stored["encrypted_payload"])

    assert result.outcome == CheckInOutcome.CLAIMED
    assert qoder.access_tokens == ["access-qd-main", "winner-access"]
    assert payload == {
        "access_token": "winner-access",
        "refresh_token": "winner-refresh",
    }
    await executor.close()


async def _seed(repository: AccountRepository, vault: CredentialVault) -> None:
    async with repository.transaction():
        await repository.upsert_account(
            provider="qoder",
            account_id="qd-main",
            label="qd-main",
            source="manual",
            enabled=True,
        )
        await repository.upsert_purpose(
            provider="qoder",
            account_id="qd-main",
            purpose="checkin",
            enabled=True,
            status="active",
            verification_status="verified",
            capabilities=["checkin.qoder"],
        )
        await repository.upsert_credential(
            provider="qoder",
            account_id="qd-main",
            purpose="checkin",
            mode="access_refresh",
            encrypted_payload=vault.encrypt(
                {
                    "access_token": "access-qd-main",
                    "refresh_token": "refresh-qd-main",
                }
            ),
            has_refresh_token=True,
        )


def _executor(
    repository: AccountRepository,
    vault: CredentialVault,
    registry: AccountRegistry,
    *,
    qoder: _RefreshingQoderClient,
) -> CheckinExecutor:
    return CheckinExecutor(
        settings=Settings(codebuddy_tokens=[], qoder_tokens=[]),
        repo=repository,
        registry=registry,
        resolver=CredentialResolver(repository, vault, registry),
        vault=vault,
        workbuddy=_UnusedWorkBuddy(),  # type: ignore[arg-type]
        qoder=qoder,  # type: ignore[arg-type]
    )
