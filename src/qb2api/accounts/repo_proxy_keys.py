"""Proxy API Key persistence isolated from general control metadata."""

from __future__ import annotations

import json
from typing import Any

from .schema import now_iso


class ProxyKeyRepositoryMixin:
    async def list_proxy_api_keys(self) -> list[dict[str, Any]]:
        async with self._operation() as db:
            cursor = await db.execute(
                """
                SELECT key_id, name, scopes_json, enabled, created_at,
                       last_used_at, expires_at, revoked_at
                FROM proxy_api_keys ORDER BY created_at DESC
                """
            )
            rows = await cursor.fetchall()
        return [_proxy_key_row(row) for row in rows]

    async def list_proxy_key_runtime_records(self) -> list[dict[str, Any]]:
        """Return hash-only runtime records, including revoked history."""
        async with self._operation() as db:
            cursor = await db.execute(
                """
                SELECT key_id, key_hash, expires_at, enabled, revoked_at
                FROM proxy_api_keys ORDER BY created_at
                """
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def create_proxy_api_key(
        self,
        *,
        key_id: str,
        name: str,
        key_hash: str,
        expires_at: str | None,
    ) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO proxy_api_keys
                    (key_id, name, key_hash, scopes_json, enabled, created_at, expires_at)
                VALUES (?, ?, ?, '["proxy"]', 1, ?, ?)
                """,
                (key_id, name, key_hash, now_iso(), expires_at),
            )

    async def revoke_proxy_api_key(self, key_id: str) -> bool:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                """
                UPDATE proxy_api_keys
                SET enabled=0, revoked_at=?
                WHERE key_id=? AND enabled=1 AND revoked_at IS NULL
                """,
                (now_iso(), key_id),
            )
            return cursor.rowcount == 1


def _proxy_key_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["enabled"] = bool(result.get("enabled"))
    try:
        result["scopes"] = json.loads(result.pop("scopes_json"))
    except (TypeError, json.JSONDecodeError):
        result["scopes"] = []
        result.pop("scopes_json", None)
    return result
