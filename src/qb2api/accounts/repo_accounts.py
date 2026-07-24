"""Account, purpose, and credential persistence methods."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from .schema import UPSERT_ACCOUNT, UPSERT_PURPOSE, now_iso


class AccountRepositoryMixin:
    async def upsert_account(
        self,
        *,
        provider: str,
        account_id: str,
        label: str,
        source: str,
        enabled: bool,
        masked_identity: str | None = None,
        identity_hash: str | None = None,
    ) -> None:
        now = now_iso()
        values = (
            provider,
            account_id,
            label,
            source,
            int(enabled),
            masked_identity,
            identity_hash,
            now,
            now,
        )
        async with self._operation(write=True) as db:
            await db.execute(UPSERT_ACCOUNT, values)

    async def list_accounts(self, provider: str | None = None) -> list[dict[str, Any]]:
        async with self._operation() as db:
            if provider:
                cursor = await db.execute(
                    "SELECT * FROM accounts WHERE provider=? ORDER BY provider, account_id",
                    (provider,),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM accounts ORDER BY provider, account_id"
                )
            rows = await cursor.fetchall()
        return [self._account_row(row) for row in rows]

    async def delete_account(self, provider: str, account_id: str) -> bool:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                "DELETE FROM accounts WHERE provider=? AND account_id=?",
                (provider, account_id),
            )
        return cursor.rowcount > 0

    async def upsert_purpose(
        self,
        *,
        provider: str,
        account_id: str,
        purpose: str,
        enabled: bool,
        status: str,
        verification_status: str,
        capabilities: list[str] | None = None,
        verified_at: str | None = None,
        expires_at: str | None = None,
        last_success_at: str | None = None,
        failure_count: int = 0,
        last_error: str | None = None,
    ) -> None:
        values = self._purpose_values(
            provider=provider,
            account_id=account_id,
            purpose=purpose,
            enabled=enabled,
            status=status,
            verification_status=verification_status,
            capabilities=capabilities,
            verified_at=verified_at,
            expires_at=expires_at,
            last_success_at=last_success_at,
            failure_count=failure_count,
            last_error=last_error,
        )
        async with self._operation(write=True) as db:
            await db.execute(UPSERT_PURPOSE, values)

    async def list_purposes(
        self, provider: str, account_id: str
    ) -> list[dict[str, Any]]:
        async with self._operation() as db:
            cursor = await db.execute(
                """
                SELECT * FROM account_purposes
                WHERE provider=? AND account_id=?
                ORDER BY purpose
                """,
                (provider, account_id),
            )
            rows = await cursor.fetchall()
        return [self._purpose_row(row) for row in rows]

    @staticmethod
    def _purpose_values(**values: Any) -> tuple[Any, ...]:
        return (
            values["provider"],
            values["account_id"],
            values["purpose"],
            int(values["enabled"]),
            values["status"],
            values["verification_status"],
            json.dumps(values["capabilities"] or [], ensure_ascii=False),
            values["verified_at"],
            values["expires_at"],
            values["last_success_at"],
            values["failure_count"],
            values["last_error"],
            now_iso(),
        )

    @staticmethod
    def _account_row(row: aiosqlite.Row) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result.get("enabled"))
        return result

    @staticmethod
    def _purpose_row(row: aiosqlite.Row) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result.get("enabled"))
        raw = result.pop("capabilities_json", "[]") or "[]"
        try:
            result["capabilities"] = json.loads(raw)
        except json.JSONDecodeError:
            result["capabilities"] = []
        return result
