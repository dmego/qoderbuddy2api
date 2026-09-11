"""Per (provider, account, model) blocks: persistence, pool enforcement, snapshot."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from qb2api.openai import ChatCompletionRequest
from qb2api.providers.base import Provider
from qb2api.providers.lb import DynamicProviderPool
from qb2api.runtime_snapshot import AccountModelBlock, RuntimeSnapshot


class FakeProvider(Provider):
    name = "fake"

    def __init__(self, account_id: str) -> None:
        self.account_id = account_id
        self.calls = 0

    async def complete(self, request: ChatCompletionRequest) -> dict:
        self.calls += 1
        return {"id": self.account_id}

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        self.calls += 1
        yield b"data: first\n\n"
        yield b"data: [DONE]\n\n"

    async def close(self) -> None:
        pass


def _req(model: str = "hy3") -> ChatCompletionRequest:
    return ChatCompletionRequest(model=model, messages=[{"role": "user", "content": "hi"}])


async def _pool(*providers: FakeProvider) -> DynamicProviderPool:
    pool = DynamicProviderPool(name="codebuddy")
    await pool.update_slots({provider.account_id: provider for provider in providers})
    return pool


class TestPoolHardBlocks:
    @pytest.mark.asyncio
    async def test_manual_block_excludes_the_account_for_that_model_only(self):
        blocked = FakeProvider("a")
        healthy = FakeProvider("b")
        pool = await _pool(blocked, healthy)
        pool.set_hard_blocks({("a", "hy3"): "管理员手动停用"})

        for _ in range(3):
            assert (await pool.complete(_req("hy3")))["id"] == "b"
        assert blocked.calls == 0

        # The block is model-scoped: another model still uses the account.
        assert (await pool.complete(_req("other")))["id"] in {"a", "b"}

    @pytest.mark.asyncio
    async def test_manual_block_is_never_canaried_even_when_nothing_else_is_healthy(self):
        """The fallback path retries cooling accounts but must still skip hard blocks."""
        blocked = FakeProvider("a")
        cooling = FakeProvider("b")
        pool = await _pool(blocked, cooling)
        pool.set_hard_blocks({("a", "hy3"): "管理员手动停用"})
        pool._failed["b"] = time.monotonic() + 600  # everything else is cooling

        handle = await pool._acquire(set(), advance=True, model="hy3")

        # The cooled account is canaried; the manually blocked one is not.
        assert handle is not None and handle.key == "b"
        assert blocked.calls == 0

    @pytest.mark.asyncio
    async def test_manual_block_yields_no_handle_when_it_is_the_only_account(self):
        pool = await _pool(FakeProvider("a"))
        pool.set_hard_blocks({("a", "hy3"): "管理员手动停用"})

        assert await pool._acquire(set(), advance=True, model="hy3") is None

    @pytest.mark.asyncio
    async def test_replacing_hard_blocks_reenables_the_account(self):
        blocked = FakeProvider("a")
        pool = await _pool(blocked)
        pool.set_hard_blocks({("a", "hy3"): "管理员手动停用"})
        assert await pool._acquire(set(), advance=True, model="hy3") is None

        pool.set_hard_blocks({})
        handle = await pool._acquire(set(), advance=True, model="hy3")
        assert handle is not None and handle.key == "a"

    @pytest.mark.asyncio
    async def test_manual_block_is_reported_with_its_reason(self):
        pool = await _pool(FakeProvider("a"))
        pool.set_hard_blocks({("a", "hy3"): "管理员手动停用"})

        blocks = pool.quota_blocks()

        assert len(blocks) == 1
        assert blocks[0]["source"] == "manual"
        assert blocks[0]["reason"] == "管理员手动停用"
        assert blocks[0]["blocked_until"] == ""
        assert blocks[0]["model_id"] == "hy3"

    @pytest.mark.asyncio
    async def test_auto_and_manual_blocks_are_both_reported(self):
        pool = DynamicProviderPool(name="codebuddy")
        await pool.update_slots({"a": FakeProvider("a"), "b": FakeProvider("b")})
        pool.set_hard_blocks({("a", "hy3"): "管理员手动停用"})
        pool._mark_model_blocked(
            "b", "hy3", datetime.now(UTC) + timedelta(seconds=120), reason="配额受限"
        )

        by_account = {block["account_id"]: block for block in pool.quota_blocks()}

        assert by_account["a"]["source"] == "manual"
        assert by_account["b"]["source"] == "auto"
        assert by_account["b"]["reason"] == "配额受限"
        assert by_account["b"]["blocked_until"].endswith("Z")

    @pytest.mark.asyncio
    async def test_retiring_a_slot_drops_its_hard_blocks(self):
        pool = await _pool(FakeProvider("a"))
        pool.set_hard_blocks({("a", "hy3"): "管理员手动停用"})
        await pool.update_slots({})

        assert pool.quota_blocks() == []
        assert pool._model_blocked_hard == set()
        assert pool._model_hard_reason == {}


class TestSnapshotRoundTrip:
    def _snapshot(self, blocks: tuple[AccountModelBlock, ...]) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            snapshot_version=1,
            codebuddy_endpoint="https://copilot.tencent.com",
            qoder_timeout=300,
            models={"codebuddy": ()},
            slots=(),
            account_model_blocks=blocks,
        )

    def test_blocks_survive_a_payload_round_trip(self):
        snapshot = self._snapshot((
            AccountModelBlock("codebuddy", "cb-1", "hy3", "管理员手动停用"),
            AccountModelBlock("workbuddy_intl", "wbintl-1", "deepseek-v4.1-flash", ""),
        ))
        restored = RuntimeSnapshot.from_payload(json.loads(json.dumps(snapshot.to_payload())))

        assert restored.account_model_blocks[0] == AccountModelBlock(
            "codebuddy", "cb-1", "hy3", "管理员手动停用"
        )
        assert restored.account_model_blocks[1].provider == "workbuddy_intl"

    def test_snapshot_without_blocks_still_parses(self):
        payload = self._snapshot(()).to_payload()
        payload.pop("account_model_blocks")
        assert RuntimeSnapshot.from_payload(payload).account_model_blocks == ()

    @pytest.mark.parametrize(
        "bad",
        [
            "not-a-list",
            [{"provider": "", "account_id": "a", "model_id": "m"}],
            [{"provider": "p", "account_id": "", "model_id": "m"}],
            [{"provider": "p", "account_id": "a", "model_id": ""}],
            ["not-a-dict"],
        ],
    )
    def test_malformed_blocks_are_rejected(self, bad):
        payload = self._snapshot(()).to_payload()
        payload["account_model_blocks"] = bad
        with pytest.raises(ValueError):
            RuntimeSnapshot.from_payload(payload)


class TestWorkerAppliesBlocks:
    @pytest.mark.asyncio
    async def test_worker_runtime_pushes_blocks_into_each_pool(self):
        """Regression: a manual block that never reaches the pool does nothing."""
        from qb2api.config import Settings
        from qb2api.providers import ProviderRegistry
        from qb2api.runtime_snapshot import RuntimeSlot
        from qb2api.worker.runtime import WorkerRuntime

        registry = ProviderRegistry()
        runtime = WorkerRuntime(Settings(), registry)
        snapshot = RuntimeSnapshot(
            snapshot_version=1,
            codebuddy_endpoint="https://copilot.tencent.com",
            workbuddy_intl_endpoint="https://www.workbuddy.ai",
            qoder_timeout=300,
            models={"codebuddy": ()},
            slots=(
                RuntimeSlot("codebuddy", "cb-1", 1, "cb-token"),
                RuntimeSlot("workbuddy_intl", "wbintl-1", 1, "wb-token"),
            ),
            account_model_blocks=(
                AccountModelBlock("codebuddy", "cb-1", "hy3", "管理员手动停用"),
                AccountModelBlock("workbuddy_intl", "wbintl-1", "hy3", "管理员手动停用"),
            ),
        )

        await runtime.start(snapshot)

        assert runtime.codebuddy_pool.quota_blocks()[0]["account_id"] == "cb-1"
        assert runtime.workbuddy_intl_pool.quota_blocks()[0]["account_id"] == "wbintl-1"
        await runtime.close()


class TestRepositoryPersistence:
    @pytest.mark.asyncio
    async def test_blocks_round_trip_through_sqlite(self, tmp_path):
        from qb2api.accounts.repository import AccountRepository

        repo = AccountRepository(str(tmp_path / "qb.sqlite3"))
        await repo.connect()
        try:
            await repo.migrate()
            await repo.set_account_model_block(
                provider="codebuddy",
                account_id="cb-1",
                model_id="hy3",
                reason="管理员手动停用",
                source="manual",
            )
            await repo.set_account_model_block(
                provider="codebuddy",
                account_id="cb-2",
                model_id="hy3",
                reason="配额受限",
                source="auto",
                blocked_until="2026-09-11T10:00:47+00:00",
            )

            rows = await repo.list_account_model_blocks()
            assert {row["account_id"] for row in rows} == {"cb-1", "cb-2"}

            # Re-blocking updates in place rather than duplicating.
            await repo.set_account_model_block(
                provider="codebuddy", account_id="cb-1", model_id="hy3",
                reason="换了个原因", source="manual",
            )
            rows = await repo.list_account_model_blocks("hy3")
            manual = next(row for row in rows if row["account_id"] == "cb-1")
            assert manual["reason"] == "换了个原因"
            assert len(rows) == 2

            assert await repo.clear_account_model_block(
                provider="codebuddy", account_id="cb-1", model_id="hy3"
            )
            assert not await repo.clear_account_model_block(
                provider="codebuddy", account_id="cb-1", model_id="hy3"
            )
        finally:
            await repo.close()

    @pytest.mark.asyncio
    async def test_expired_auto_blocks_are_purged_but_manual_ones_survive(self, tmp_path):
        from qb2api.accounts.repository import AccountRepository

        repo = AccountRepository(str(tmp_path / "qb.sqlite3"))
        await repo.connect()
        try:
            await repo.migrate()
            await repo.set_account_model_block(
                provider="codebuddy", account_id="cb-auto", model_id="hy3",
                reason="配额受限", source="auto",
                blocked_until="2020-01-01T00:00:00+00:00",
            )
            await repo.set_account_model_block(
                provider="codebuddy", account_id="cb-manual", model_id="hy3",
                reason="管理员手动停用", source="manual",
            )

            removed = await repo.clear_expired_account_model_blocks(
                datetime.now(UTC).isoformat()
            )

            assert removed == 1
            remaining = await repo.list_account_model_blocks()
            assert [row["account_id"] for row in remaining] == ["cb-manual"]
        finally:
            await repo.close()


class TestNoBlockLeakBetweenModels:
    @pytest.mark.asyncio
    async def test_concurrent_models_do_not_cross_contaminate(self):
        """Blocking a on hy3 must not block a on deepseek-v4.1-flash."""
        a = FakeProvider("a")
        b = FakeProvider("b")
        pool = await _pool(a, b)
        pool.set_hard_blocks({("a", "hy3"): "管理员手动停用"})

        results = await asyncio.gather(
            *(pool.complete(_req("hy3")) for _ in range(4)),
            *(pool.complete(_req("deepseek-v4.1-flash")) for _ in range(4)),
        )
        ids = [result["id"] for result in results]
        assert ids[:4] == ["b"] * 4
        assert "a" in ids[4:]
