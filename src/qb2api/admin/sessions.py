"""Hashed admin sessions with memory and SQLite storage backends."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from qb2api.accounts.repository import AccountRepository

from .crypto import constant_time_equal, hash_token


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class SessionInfo:
    session_hash: str
    csrf_hash: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


@dataclass
class _SessionEntry:
    session_hash: str
    csrf_hash: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


class AdminSessionStore:
    """Persist only hashes; raw session and CSRF values are returned once."""

    def __init__(
        self,
        repository: AccountRepository | None = None,
        *,
        ttl_hours: int = 12,
        idle_minutes: int = 60,
        max_sessions: int = 5,
    ) -> None:
        self.ttl_hours = ttl_hours
        self.idle_minutes = idle_minutes
        self.max_sessions = max_sessions
        self._repository = repository
        self._entries: dict[str, _SessionEntry] = {}
        self._lock = asyncio.Lock()

    async def create_session(self) -> dict[str, str]:
        async with self._lock:
            await self._evict_expired_locked()
            await self._enforce_max_sessions_locked()
            session_id = secrets.token_urlsafe(32)
            csrf_token = secrets.token_urlsafe(32)
            now = _now()
            entry = _SessionEntry(
                session_hash=hash_token(session_id),
                csrf_hash=hash_token(csrf_token),
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(hours=self.ttl_hours),
            )
            await self._create_entry(entry)
        return {"session_id": session_id, "csrf_token": csrf_token}

    async def validate_session(
        self, cookie_value: str | None, *, touch: bool = True
    ) -> SessionInfo | None:
        if not cookie_value:
            return None
        async with self._lock:
            entry = await self._get_entry(hash_token(cookie_value))
            if entry is None or entry.revoked_at is not None:
                return None
            now = _now()
            if self._is_expired(entry, now):
                await self._revoke_entry(entry, now)
                return None
            if touch and now - entry.last_seen_at >= timedelta(minutes=1):
                entry.last_seen_at = now
                await self._touch_entry(entry)
            return self._session_info(entry)

    def verify_csrf(self, info: SessionInfo, csrf_token: str | None) -> bool:
        if not csrf_token:
            return False
        return constant_time_equal(hash_token(csrf_token), info.csrf_hash)

    async def rotate_csrf(self, cookie_value: str | None) -> str | None:
        info = await self.validate_session(cookie_value, touch=True)
        if info is None:
            return None
        async with self._lock:
            entry = await self._get_entry(info.session_hash)
            if entry is None or entry.revoked_at is not None:
                return None
            csrf_token = secrets.token_urlsafe(32)
            entry.csrf_hash = hash_token(csrf_token)
            await self._rotate_entry(entry)
            return csrf_token

    async def revoke_session(self, cookie_value: str) -> bool:
        async with self._lock:
            entry = await self._get_entry(hash_token(cookie_value))
            if entry is None or entry.revoked_at is not None:
                return False
            await self._revoke_entry(entry, _now())
            return True

    async def revoke_all(self) -> int:
        async with self._lock:
            now = _now()
            if self._repository is not None:
                return await self._repository.revoke_all_admin_sessions(now.isoformat())
            count = 0
            for entry in self._entries.values():
                if entry.revoked_at is None:
                    entry.revoked_at = now
                    count += 1
            return count

    async def active_count(self) -> int:
        async with self._lock:
            await self._evict_expired_locked()
            return sum(entry.revoked_at is None for entry in await self._all_entries())

    async def _create_entry(self, entry: _SessionEntry) -> None:
        if self._repository is None:
            self._entries[entry.session_hash] = entry
            return
        await self._repository.create_admin_session(
            session_hash=entry.session_hash,
            csrf_hash=entry.csrf_hash,
            created_at=entry.created_at.isoformat(),
            last_seen_at=entry.last_seen_at.isoformat(),
            expires_at=entry.expires_at.isoformat(),
        )

    async def _get_entry(self, session_hash: str) -> _SessionEntry | None:
        if self._repository is None:
            return self._entries.get(session_hash)
        row = await self._repository.get_admin_session(session_hash)
        return self._from_row(row) if row else None

    async def _all_entries(self) -> list[_SessionEntry]:
        if self._repository is None:
            return list(self._entries.values())
        rows = await self._repository.list_admin_sessions()
        return [self._from_row(row) for row in rows]

    async def _touch_entry(self, entry: _SessionEntry) -> None:
        if self._repository is None:
            return
        await self._repository.touch_admin_session(
            session_hash=entry.session_hash,
            last_seen_at=entry.last_seen_at.isoformat(),
        )

    async def _rotate_entry(self, entry: _SessionEntry) -> None:
        if self._repository is None:
            return
        await self._repository.rotate_admin_csrf(
            session_hash=entry.session_hash,
            csrf_hash=entry.csrf_hash,
        )

    async def _revoke_entry(self, entry: _SessionEntry, now: datetime) -> None:
        entry.revoked_at = now
        if self._repository is not None:
            await self._repository.revoke_admin_session(
                session_hash=entry.session_hash,
                revoked_at=now.isoformat(),
            )

    async def _evict_expired_locked(self) -> None:
        now = _now()
        for entry in await self._all_entries():
            if entry.revoked_at is None and self._is_expired(entry, now):
                await self._revoke_entry(entry, now)

    async def _enforce_max_sessions_locked(self) -> None:
        active = [
            entry for entry in await self._all_entries() if entry.revoked_at is None
        ]
        overflow = len(active) - self.max_sessions + 1
        if overflow <= 0:
            return
        now = _now()
        for entry in sorted(active, key=lambda item: item.created_at)[:overflow]:
            await self._revoke_entry(entry, now)

    def _is_expired(self, entry: _SessionEntry, now: datetime) -> bool:
        idle_limit = timedelta(minutes=self.idle_minutes)
        return entry.expires_at <= now or entry.last_seen_at + idle_limit <= now

    @staticmethod
    def _session_info(entry: _SessionEntry) -> SessionInfo:
        return SessionInfo(
            session_hash=entry.session_hash,
            csrf_hash=entry.csrf_hash,
            created_at=entry.created_at,
            last_seen_at=entry.last_seen_at,
            expires_at=entry.expires_at,
        )

    @staticmethod
    def _from_row(row: dict[str, str | None]) -> _SessionEntry:
        revoked = row.get("revoked_at")
        return _SessionEntry(
            session_hash=str(row["session_hash"]),
            csrf_hash=str(row["csrf_hash"]),
            created_at=_parse_time(str(row["created_at"])),
            last_seen_at=_parse_time(str(row["last_seen_at"])),
            expires_at=_parse_time(str(row["expires_at"])),
            revoked_at=_parse_time(str(revoked)) if revoked else None,
        )
