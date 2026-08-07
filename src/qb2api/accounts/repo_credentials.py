"""Credential persistence with compare-and-swap versioning."""

from __future__ import annotations

from typing import Any

import aiosqlite

from .schema import INSERT_CREDENTIAL, UPDATE_CREDENTIAL, now_iso


class CredentialVersionConflict(Exception):
    """The durable credential version changed before a CAS write."""


class CredentialRepositoryMixin:
    async def upsert_credential(
        self,
        *,
        provider: str,
        account_id: str,
        purpose: str,
        mode: str,
        encrypted_payload: str,
        has_refresh_token: bool = False,
        expires_at: str | None = None,
        fingerprint_hmac: str | None = None,
        payload_version: int = 1,
        expected_version: int | None = None,
    ) -> int:
        values = {
            "provider": provider,
            "account_id": account_id,
            "purpose": purpose,
            "mode": mode,
            "encrypted_payload": encrypted_payload,
            "has_refresh_token": has_refresh_token,
            "expires_at": expires_at,
            "fingerprint_hmac": fingerprint_hmac,
            "payload_version": payload_version,
            "expected_version": expected_version,
        }
        async with self._operation(write=True) as db:
            existing = await self._fetch_credential(db=db, **_credential_key(values))
            if existing is None:
                return await self._insert_credential(db=db, **values)
            return await self._update_credential(db=db, existing=existing, **values)

    async def get_credential(
        self, provider: str, account_id: str, purpose: str
    ) -> dict[str, Any] | None:
        async with self._operation() as db:
            return await self._fetch_credential(
                db=db,
                provider=provider,
                account_id=account_id,
                purpose=purpose,
            )

    async def delete_credential(self, provider: str, account_id: str, purpose: str) -> bool:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                "DELETE FROM credentials WHERE provider=? AND account_id=? AND purpose=?",
                (provider, account_id, purpose),
            )
        return cursor.rowcount == 1

    async def list_credential_metadata(self, provider: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT provider, account_id, purpose, mode, payload_version,
                   credential_version, expires_at, has_refresh_token, updated_at
            FROM credentials
        """
        params: tuple[str, ...] = ()
        if provider:
            query += " WHERE provider=?"
            params = (provider,)
        query += " ORDER BY provider, account_id, purpose"
        async with self._operation() as db:
            cursor = await db.execute(query, params)
            rows = await cursor.fetchall()
        return [_credential_metadata_row(row) for row in rows]

    async def _fetch_credential(
        self,
        *,
        db: aiosqlite.Connection,
        provider: str,
        account_id: str,
        purpose: str,
    ) -> dict[str, Any] | None:
        cursor = await db.execute(
            """
            SELECT * FROM credentials
            WHERE provider=? AND account_id=? AND purpose=?
            """,
            (provider, account_id, purpose),
        )
        row = await cursor.fetchone()
        return _credential_row(row) if row else None

    async def _insert_credential(self, *, db: aiosqlite.Connection, **values: Any) -> int:
        expected = values.pop("expected_version")
        if expected not in (None, 0):
            raise _version_conflict(values, f"expected v{expected}, row missing")
        try:
            await db.execute(INSERT_CREDENTIAL, _credential_insert_values(values))
        except aiosqlite.IntegrityError as error:
            if expected == 0:
                raise _version_conflict(values, "concurrent insert lost CAS") from error
            raise
        return 1

    async def _update_credential(
        self,
        *,
        db: aiosqlite.Connection,
        existing: dict[str, Any],
        **values: Any,
    ) -> int:
        current = int(existing.get("credential_version") or 1)
        expected = values.pop("expected_version")
        if expected is not None and current != expected:
            raise _version_conflict(values, f"expected v{expected}, got v{current}")
        new_version = current + 1
        cursor = await db.execute(
            UPDATE_CREDENTIAL,
            _credential_update_values(values, new_version, current),
        )
        if cursor.rowcount != 1:
            raise _version_conflict(values, f"concurrent update lost CAS to v{new_version}")
        return new_version


def _credential_key(values: dict[str, Any]) -> dict[str, str]:
    return {key: values[key] for key in ("provider", "account_id", "purpose")}


def _credential_insert_values(values: dict[str, Any]) -> tuple[Any, ...]:
    return (
        values["provider"],
        values["account_id"],
        values["purpose"],
        values["mode"],
        values["encrypted_payload"],
        values["payload_version"],
        values["fingerprint_hmac"],
        values["expires_at"],
        int(values["has_refresh_token"]),
        now_iso(),
    )


def _credential_update_values(
    values: dict[str, Any], new_version: int, current_version: int
) -> tuple[Any, ...]:
    return (
        values["mode"],
        values["encrypted_payload"],
        values["payload_version"],
        new_version,
        values["fingerprint_hmac"],
        values["expires_at"],
        int(values["has_refresh_token"]),
        now_iso(),
        values["provider"],
        values["account_id"],
        values["purpose"],
        current_version,
    )


def _version_conflict(values: dict[str, Any], detail: str) -> CredentialVersionConflict:
    key = f"{values['provider']}/{values['account_id']}/{values['purpose']}"
    return CredentialVersionConflict(f"{key}: {detail}")


def _credential_metadata_row(row: aiosqlite.Row) -> dict[str, Any]:
    result = dict(row)
    result["has_refresh_token"] = bool(result["has_refresh_token"])
    return result


def _credential_row(row: aiosqlite.Row) -> dict[str, Any]:
    result = dict(row)
    result["has_refresh_token"] = bool(result.get("has_refresh_token"))
    return result
