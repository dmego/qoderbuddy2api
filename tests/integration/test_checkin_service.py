"""Check-in coordinator integration contracts."""

from __future__ import annotations

import asyncio

import pytest
from cryptography.fernet import Fernet

from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.accounts.vault import CredentialVault
from qb2api.checkin.models import CheckInOutcome, CheckInResult
from qb2api.checkin.service import CheckinInProgressError, CheckinService
from qb2api.config import Settings


class _SequenceClient:
    def __init__(
        self,
        provider: str,
        results: list[CheckInResult | BaseException],
    ) -> None:
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


class _BlockingClient(_SequenceClient):
    def __init__(self) -> None:
        super().__init__("codebuddy", [])
        self.started = asyncio.Event()

    async def checkin(self, **_values) -> CheckInResult:
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.fixture
async def checkin_context(tmp_path):
    repository = AccountRepository(str(tmp_path / "accounts.sqlite3"))
    await repository.connect()
    await repository.migrate()
    vault = CredentialVault(Fernet.generate_key().decode())
    yield repository, vault
    await repository.close()


@pytest.mark.asyncio
async def test_scheduler_filters_disabled_provider(checkin_context) -> None:
    repository, vault = checkin_context
    await _seed(repository, vault, "codebuddy", "cb-main")
    await _seed(repository, vault, "qoder", "qd-main")
    registry = await _registry(repository, vault)
    workbuddy = _SequenceClient("codebuddy", [_success("codebuddy", "cb-main")])
    qoder = _SequenceClient("qoder", [_success("qoder", "qd-main")])
    service = _service(
        repository,
        vault,
        registry,
        workbuddy=workbuddy,
        qoder=qoder,
        codebuddy_enabled=False,
        qoder_enabled=True,
    )

    batch = await service.run_batch(trigger="scheduler")

    assert [item["account_id"] for item in batch.results] == ["qd-main"]
    assert workbuddy.calls == 0
    assert qoder.calls == 1
    await service.close()


@pytest.mark.asyncio
async def test_transient_result_retries_and_persists_attempt_count(
    checkin_context,
) -> None:
    repository, vault = checkin_context
    await _seed(repository, vault, "codebuddy", "cb-main")
    registry = await _registry(repository, vault)
    workbuddy = _SequenceClient(
        "codebuddy",
        [
            CheckInResult(
                outcome=CheckInOutcome.TRANSIENT_ERROR,
                provider="codebuddy",
                account_id="cb-main",
                http_status=503,
            ),
            _success("codebuddy", "cb-main"),
        ],
    )
    qoder = _SequenceClient("qoder", [])
    service = _service(
        repository,
        vault,
        registry,
        workbuddy=workbuddy,
        qoder=qoder,
        codebuddy_enabled=True,
        qoder_enabled=False,
        retry_limit=1,
    )

    batch = await service.run_batch(trigger="scheduler")
    attempts = await repository.list_checkin_attempts(batch.run_id)

    assert workbuddy.calls == 2
    assert attempts[0]["attempts"] == 2
    assert attempts[0]["outcome"] == CheckInOutcome.CLAIMED.value
    await service.close()


@pytest.mark.asyncio
async def test_cancelled_batch_does_not_leave_running_row(checkin_context) -> None:
    repository, vault = checkin_context
    await _seed(repository, vault, "codebuddy", "cb-main")
    registry = await _registry(repository, vault)
    workbuddy = _BlockingClient()
    qoder = _SequenceClient("qoder", [])
    service = _service(
        repository,
        vault,
        registry,
        workbuddy=workbuddy,
        qoder=qoder,
        codebuddy_enabled=True,
        qoder_enabled=False,
    )
    task = asyncio.create_task(service.run_batch(trigger="scheduler"))
    await workbuddy.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    cursor = await repository.db.execute("SELECT status FROM checkin_runs")
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "cancelled"
    await service.close()


@pytest.mark.asyncio
async def test_started_batch_returns_durable_operation_and_closes_cleanly(checkin_context) -> None:
    repository, vault = checkin_context
    await _seed(repository, vault, "codebuddy", "cb-main")
    registry = await _registry(repository, vault)
    workbuddy = _BlockingClient()
    service = _service(
        repository, vault, registry,
        workbuddy=workbuddy,
        qoder=_SequenceClient("qoder", []),
        codebuddy_enabled=True, qoder_enabled=False,
    )

    run_id = await service.start_batch(trigger="manual", skip_already_done=False)
    await workbuddy.started.wait()
    assert service.active_run_id == run_id

    with pytest.raises(CheckinInProgressError, match="checkin_run_in_progress"):
        await service.start_batch(trigger="manual")

    await service.close()
    run = await repository.get_checkin_run(run_id)
    assert run is not None
    assert run["status"] == "cancelled"


@pytest.mark.asyncio
async def test_batch_isolates_account_failure_and_preserves_chat_purpose(
    checkin_context,
) -> None:
    repository, vault = checkin_context
    await _seed(repository, vault, "codebuddy", "cb-a")
    await _seed(repository, vault, "codebuddy", "cb-b")
    registry = await _registry(repository, vault)
    workbuddy = _SequenceClient(
        "codebuddy",
        [RuntimeError("account-a-failed"), _success("codebuddy", "cb-b")],
    )
    service = _service(
        repository,
        vault,
        registry,
        workbuddy=workbuddy,
        qoder=_SequenceClient("qoder", []),
        codebuddy_enabled=True,
        qoder_enabled=False,
    )

    batch = await service.run_batch(trigger="scheduler")

    assert [item["account_id"] for item in batch.results] == ["cb-a", "cb-b"]
    assert [item["outcome"] for item in batch.results] == [
        CheckInOutcome.FAILED.value,
        CheckInOutcome.CLAIMED.value,
    ]
    for account_id in ("cb-a", "cb-b"):
        purposes = await repository.list_purposes("codebuddy", account_id)
        chat = next(item for item in purposes if item["purpose"] == "chat")
        assert chat["status"] == "active"
        assert chat["verification_status"] == "not_required"
    await service.close()


async def _seed(
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
            provider=provider,
            account_id=account_id,
            label=account_id,
            source="manual",
            enabled=True,
        )
        await repository.upsert_purpose(
            provider=provider,
            account_id=account_id,
            purpose="chat",
            enabled=True,
            status="active",
            verification_status="not_required",
            capabilities=["proxy.chat"],
        )
        await repository.upsert_purpose(
            provider=provider,
            account_id=account_id,
            purpose="checkin",
            enabled=True,
            status="active",
            verification_status="verified",
            capabilities=[f"checkin.{provider}"],
        )
        await repository.upsert_credential(
            provider=provider,
            account_id=account_id,
            purpose="checkin",
            mode="access_refresh" if provider == "qoder" else "bearer",
            encrypted_payload=vault.encrypt(payload),
            has_refresh_token=provider == "qoder",
        )


async def _registry(
    repository: AccountRepository,
    vault: CredentialVault,
) -> AccountRegistry:
    registry = AccountRegistry(repository, vault)
    await registry.rebuild()
    return registry


def _service(
    repository: AccountRepository,
    vault: CredentialVault,
    registry: AccountRegistry,
    *,
    workbuddy,
    qoder,
    codebuddy_enabled: bool,
    qoder_enabled: bool,
    retry_limit: int = 0,
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
        registry=registry,
        resolver=CredentialResolver(repository, vault, registry),
        vault=vault,
        workbuddy=workbuddy,
        qoder=qoder,
    )


def _success(provider: str, account_id: str) -> CheckInResult:
    return CheckInResult(
        outcome=CheckInOutcome.CLAIMED,
        provider=provider,
        account_id=account_id,
        http_status=200,
    )
