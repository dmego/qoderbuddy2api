"""Per-model provider routing policy persistence."""

from __future__ import annotations

from typing import Any

from .schema import now_iso


class RoutePolicyRepositoryMixin:
    async def list_route_policies(
        self,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self._operation() as db:
            if model_id:
                cursor = await db.execute(
                    "SELECT * FROM model_route_policies WHERE model_id=? ORDER BY priority, provider",
                    (model_id,),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM model_route_policies ORDER BY model_id, priority, provider"
                )
            rows = await cursor.fetchall()
        return [self._route_policy_row(row) for row in rows]

    async def replace_route_policies(
        self,
        model_id: str,
        policies: list[dict[str, Any]],
    ) -> None:
        """Replace every stored route for one model in a single transaction."""
        now = now_iso()
        async with self._operation(write=True) as db:
            await db.execute("DELETE FROM model_route_policies WHERE model_id=?", (model_id,))
            await db.executemany(
                """
                INSERT INTO model_route_policies
                    (model_id, provider, priority, weight, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        model_id,
                        policy["provider"],
                        int(policy["priority"]),
                        int(policy["weight"]),
                        int(bool(policy["enabled"])),
                        now,
                    )
                    for policy in policies
                ],
            )

    async def delete_route_policies(self, model_id: str) -> int:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                "DELETE FROM model_route_policies WHERE model_id=?", (model_id,)
            )
        return cursor.rowcount

    @staticmethod
    def _route_policy_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result.get("enabled"))
        result["priority"] = int(result.get("priority") or 0)
        result["weight"] = int(result.get("weight") or 0)
        return result


class AccountModelBlockRepositoryMixin:
    """Durable per (provider, account, model) blocks.

    Two sources share one table: ``manual`` rows are set by the administrator
    and never expire; ``auto`` rows are written when upstream reports a quota
    reset time. Keeping both here means the routing console can explain why an
    account is unavailable without asking the Worker.
    """

    async def list_account_model_blocks(
        self,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self._operation() as db:
            if model_id:
                cursor = await db.execute(
                    "SELECT * FROM account_model_blocks WHERE model_id=? "
                    "ORDER BY provider, account_id",
                    (model_id,),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM account_model_blocks ORDER BY provider, account_id, model_id"
                )
            rows = await cursor.fetchall()
        return [self._block_row(row) for row in rows]

    async def set_account_model_block(
        self,
        *,
        provider: str,
        account_id: str,
        model_id: str,
        reason: str = "",
        source: str = "manual",
        blocked_until: str | None = None,
    ) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO account_model_blocks
                    (provider, account_id, model_id, reason, source, blocked_until, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, account_id, model_id) DO UPDATE SET
                    reason=excluded.reason,
                    source=excluded.source,
                    blocked_until=excluded.blocked_until,
                    updated_at=excluded.updated_at
                """,
                (provider, account_id, model_id, reason, source, blocked_until, now_iso()),
            )

    async def clear_account_model_block(
        self,
        *,
        provider: str,
        account_id: str,
        model_id: str,
    ) -> bool:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                "DELETE FROM account_model_blocks "
                "WHERE provider=? AND account_id=? AND model_id=?",
                (provider, account_id, model_id),
            )
        return cursor.rowcount > 0

    async def clear_expired_account_model_blocks(self, now: str) -> int:
        """Drop auto blocks whose reset time has passed; manual rows persist."""
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                "DELETE FROM account_model_blocks "
                "WHERE source='auto' AND blocked_until IS NOT NULL AND blocked_until <= ?",
                (now,),
            )
        return cursor.rowcount

    @staticmethod
    def _block_row(row: Any) -> dict[str, Any]:
        result = dict(row)
        result["reason"] = str(result.get("reason") or "")
        result["source"] = str(result.get("source") or "manual")
        return result
