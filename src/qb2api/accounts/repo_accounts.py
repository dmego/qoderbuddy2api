"""Account, purpose, and credential persistence methods."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from .schema import (
    INSERT_CREDENTIAL,
    UPDATE_CREDENTIAL,
    UPSERT_ACCOUNT,
    UPSERT_PURPOSE,
    now_iso,
)


class CredentialVersionConflict(Exception):
    """The durable credential version changed before a CAS write."""


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
        async with self._operation(write=True) as db:
            existing = await self._fetch_credential(db, provider, account_id, purpose)
            if existing is None:
                return await self._insert_credential(
                    db=db,
                    provider=provider,
                    account_id=account_id,
                    purpose=purpose,
                    mode=mode,
                    encrypted_payload=encrypted_payload,
                    has_refresh_token=has_refresh_token,
                    expires_at=expires_at,
                    fingerprint_hmac=fingerprint_hmac,
                    payload_version=payload_version,
                    expected_version=expected_version,
                )
            return await self._update_credential(
                db=db,
                existing=existing,
                provider=provider,
                account_id=account_id,
                purpose=purpose,
                mode=mode,
                encrypted_payload=encrypted_payload,
                has_refresh_token=has_refresh_token,
                expires_at=expires_at,
                fingerprint_hmac=fingerprint_hmac,
                payload_version=payload_version,
                expected_version=expected_version,
            )

    async def get_credential(
        self, provider: str, account_id: str, purpose: str
    ) -> dict[str, Any] | None:
        async with self._operation() as db:
            return await self._fetch_credential(db, provider, account_id, purpose)

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
        result = [dict(row) for row in rows]
        for item in result:
            item["has_refresh_token"] = bool(item["has_refresh_token"])
        return result

    async def _fetch_credential(
        self,
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
        return self._credential_row(row) if row else None

    async def _insert_credential(self, *, db: aiosqlite.Connection, **values: Any) -> int:
        expected = values.pop("expected_version")
        if expected not in (None, 0):
            raise self._version_conflict(values, f"expected v{expected}, row missing")
        params = (
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
        try:
            await db.execute(INSERT_CREDENTIAL, params)
        except aiosqlite.IntegrityError as error:
            if expected == 0:
                raise self._version_conflict(values, "concurrent insert lost CAS") from error
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
            detail = f"expected v{expected}, got v{current}"
            raise self._version_conflict(values, detail)
        new_version = current + 1
        params = self._credential_update_values(values, new_version, current)
        cursor = await db.execute(UPDATE_CREDENTIAL, params)
        if cursor.rowcount != 1:
            detail = f"concurrent update lost CAS to v{new_version}"
            raise self._version_conflict(values, detail)
        return new_version

    @staticmethod
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

    @staticmethod
    def _version_conflict(values: dict[str, Any], detail: str) -> Exception:
        key = f"{values['provider']}/{values['account_id']}/{values['purpose']}"
        return CredentialVersionConflict(f"{key}: {detail}")

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

    @staticmethod
    def _credential_row(row: aiosqlite.Row) -> dict[str, Any]:
        result = dict(row)
        result["has_refresh_token"] = bool(result.get("has_refresh_token"))
        return result
