"""Transactional account repository contracts."""

from __future__ import annotations

import stat

import pytest

from qb2api.accounts.repository import AccountRepository


async def _write_account_bundle(repository: AccountRepository) -> None:
    await repository.upsert_account(
        provider="codebuddy",
        account_id="cb-main",
        label="main",
        source="oauth",
        enabled=True,
    )
    await repository.upsert_purpose(
        provider="codebuddy",
        account_id="cb-main",
        purpose="chat",
        enabled=True,
        status="active",
        verification_status="verified",
        capabilities=["proxy.chat"],
    )
    await repository.upsert_credential(
        provider="codebuddy",
        account_id="cb-main",
        purpose="chat",
        mode="oauth",
        encrypted_payload="ciphertext",
        has_refresh_token=True,
    )


@pytest.mark.asyncio
async def test_transaction_rolls_back_entire_account_bundle(tmp_path) -> None:
    repository = AccountRepository(str(tmp_path / "accounts.sqlite3"))
    await repository.connect()
    await repository.migrate()

    with pytest.raises(RuntimeError, match="injected failure"):
        async with repository.transaction():
            await _write_account_bundle(repository)
            raise RuntimeError("injected failure")

    assert await repository.list_accounts() == []
    await repository.close()


@pytest.mark.asyncio
async def test_transaction_commits_entire_account_bundle(tmp_path) -> None:
    repository = AccountRepository(str(tmp_path / "accounts.sqlite3"))
    await repository.connect()
    await repository.migrate()

    async with repository.transaction():
        await _write_account_bundle(repository)

    accounts = await repository.list_accounts()
    purposes = await repository.list_purposes("codebuddy", "cb-main")
    credential = await repository.get_credential("codebuddy", "cb-main", "chat")
    assert len(accounts) == 1
    assert [purpose["purpose"] for purpose in purposes] == ["chat"]
    assert credential is not None
    await repository.close()


@pytest.mark.asyncio
async def test_single_repository_write_still_autocommits(tmp_path) -> None:
    path = tmp_path / "accounts.sqlite3"
    first = AccountRepository(str(path))
    second = AccountRepository(str(path))
    await first.connect()
    await first.migrate()
    await second.connect()

    await first.upsert_account(
        provider="qoder",
        account_id="qd-main",
        label="main",
        source="manual",
        enabled=True,
    )

    assert len(await second.list_accounts()) == 1
    await first.close()
    await second.close()


@pytest.mark.asyncio
async def test_attempt_and_daily_state_roll_back_together(tmp_path) -> None:
    repository = AccountRepository(str(tmp_path / "accounts.sqlite3"))
    await repository.connect()
    await repository.migrate()
    await repository.create_checkin_run(
        run_id="run-1",
        local_date="2026-07-22",
        timezone="Asia/Shanghai",
    )

    with pytest.raises(RuntimeError, match="injected failure"):
        async with repository.transaction():
            await repository.upsert_checkin_attempt(
                run_id="run-1",
                provider="codebuddy",
                account_id="cb-main",
                outcome="CLAIMED",
                reward_expires_at="2026-08-05T00:00:00+00:00",
            )
            await repository.set_checkin_daily_state(
                provider="codebuddy",
                account_id="cb-main",
                local_date="2026-07-22",
                timezone="Asia/Shanghai",
                terminal_outcome="CLAIMED",
                last_run_id="run-1",
            )
            raise RuntimeError("injected failure")

    assert await repository.list_checkin_attempts("run-1") == []
    state = await repository.get_checkin_daily_state(
        "codebuddy", "cb-main", "2026-07-22", "Asia/Shanghai"
    )
    assert state is None
    await repository.close()


@pytest.mark.asyncio
async def test_checkin_attempt_persists_reward_and_quota_details(tmp_path) -> None:
    repository = AccountRepository(str(tmp_path / "accounts.sqlite3"))
    await repository.connect()
    await repository.migrate()
    await repository.create_checkin_run(
        run_id="run-1", local_date="2026-07-22", timezone="Asia/Shanghai"
    )
    await repository.upsert_checkin_attempt(
        run_id="run-1", provider="qoder", account_id="qd-main",
        outcome="CLAIMED", reward_credits=100,
        reward_expires_at="2026-08-05T00:00:00+00:00",
        quota_before={"packages": [{"name": "user", "remaining": 0}]},
        quota_after={"packages": [{"name": "user", "remaining": 100}]},
        quota_delta={"packages": [{"name": "user", "delta": 100}]},
        quota_observed_at="2026-07-22T10:00:00+00:00",
    )
    attempt = (await repository.list_checkin_attempts("run-1"))[0]
    assert attempt["reward_credits"] == 100
    assert attempt["reward_expires_at"] == "2026-08-05T00:00:00+00:00"
    assert attempt["quota_before"] == {"packages": [{"name": "user", "remaining": 0}]}
    assert attempt["quota_after"]["packages"][0]["remaining"] == 100
    assert attempt["quota_delta"]["packages"][0]["delta"] == 100
    assert attempt["quota_observed_at"] == "2026-07-22T10:00:00+00:00"
    await repository.close()


@pytest.mark.asyncio
async def test_repository_restricts_new_database_file(tmp_path) -> None:
    path = tmp_path / "accounts.sqlite3"
    repository = AccountRepository(str(path))
    await repository.connect()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    await repository.close()


@pytest.mark.asyncio
async def test_repository_restricts_existing_database_file(tmp_path) -> None:
    path = tmp_path / "accounts.sqlite3"
    path.touch()
    path.chmod(0o644)
    repository = AccountRepository(str(path))
    await repository.connect()

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    await repository.close()
