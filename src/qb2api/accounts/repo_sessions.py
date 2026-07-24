"""Hashed admin-session persistence methods."""

from __future__ import annotations

from typing import Any


class SessionRepositoryMixin:
    async def create_admin_session(
        self,
        *,
        session_hash: str,
        csrf_hash: str,
        created_at: str,
        last_seen_at: str,
        expires_at: str,
    ) -> None:
        async with self._operation(write=True) as db:
            await db.execute(
                """
                INSERT INTO admin_sessions (
                    session_hash, csrf_hash, created_at, last_seen_at,
                    expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (session_hash, csrf_hash, created_at, last_seen_at, expires_at),
            )

    async def list_admin_sessions(self) -> list[dict[str, Any]]:
        async with self._operation() as db:
            cursor = await db.execute(
                "SELECT * FROM admin_sessions ORDER BY created_at, session_hash"
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_admin_session(self, session_hash: str) -> dict[str, Any] | None:
        async with self._operation() as db:
            cursor = await db.execute(
                "SELECT * FROM admin_sessions WHERE session_hash=?",
                (session_hash,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def touch_admin_session(
        self,
        *,
        session_hash: str,
        last_seen_at: str,
    ) -> bool:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                """
                UPDATE admin_sessions SET last_seen_at=?
                WHERE session_hash=? AND revoked_at IS NULL
                """,
                (last_seen_at, session_hash),
            )
        return cursor.rowcount == 1

    async def rotate_admin_csrf(
        self,
        *,
        session_hash: str,
        csrf_hash: str,
    ) -> bool:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                """
                UPDATE admin_sessions SET csrf_hash=?
                WHERE session_hash=? AND revoked_at IS NULL
                """,
                (csrf_hash, session_hash),
            )
        return cursor.rowcount == 1

    async def revoke_admin_session(
        self,
        *,
        session_hash: str,
        revoked_at: str,
    ) -> bool:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                """
                UPDATE admin_sessions SET revoked_at=?
                WHERE session_hash=? AND revoked_at IS NULL
                """,
                (revoked_at, session_hash),
            )
        return cursor.rowcount == 1

    async def revoke_all_admin_sessions(self, revoked_at: str) -> int:
        async with self._operation(write=True) as db:
            cursor = await db.execute(
                """
                UPDATE admin_sessions SET revoked_at=?
                WHERE revoked_at IS NULL
                """,
                (revoked_at,),
            )
        return max(0, cursor.rowcount)
