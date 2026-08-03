"""Transactional async SQLite repository composition."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from qb2api.storage_permissions import ensure_private_file

from .repo_accounts import AccountRepositoryMixin
from .repo_catalog import CatalogRepositoryMixin
from .repo_checkin import CheckinRepositoryMixin
from .repo_control import ControlRepositoryMixin
from .repo_credentials import CredentialRepositoryMixin, CredentialVersionConflict
from .repo_metric_history import MetricHistoryRepositoryMixin
from .repo_metric_refresh import MetricRefreshRepositoryMixin
from .repo_proxy_keys import ProxyKeyRepositoryMixin
from .repo_service_events import ServiceEventRepositoryMixin
from .repo_sessions import SessionRepositoryMixin
from .repo_telemetry import TelemetryRepositoryMixin
from .schema import SCHEMA

__all__ = ["AccountRepository", "CredentialVersionConflict"]


class AccountRepository(
    AccountRepositoryMixin,
    CredentialRepositoryMixin,
    CatalogRepositoryMixin,
    ServiceEventRepositoryMixin,
    MetricRefreshRepositoryMixin,
    MetricHistoryRepositoryMixin,
    ControlRepositoryMixin,
    ProxyKeyRepositoryMixin,
    CheckinRepositoryMixin,
    SessionRepositoryMixin,
    TelemetryRepositoryMixin,
):
    """Own one SQLite connection and serialize every database operation."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._operation_lock = asyncio.Lock()
        self._transaction_owner: asyncio.Task[Any] | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("repository not connected; call connect() first")
        return self._db

    async def connect(self) -> None:
        if self._db is not None:
            return
        path = Path(self._db_path)
        if path.parent and str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        ensure_private_file(path)
        connection = await aiosqlite.connect(self._db_path)
        connection.row_factory = aiosqlite.Row
        await self._configure_connection(connection)
        self._db = connection

    async def migrate(self) -> None:
        async with self._operation_lock:
            await self.db.executescript(SCHEMA)
            await self._ensure_column(
                "usage_rollups",
                "token_event_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            await self._ensure_column("service_events", "in_flight", "INTEGER")
            await self._ensure_column(
                "usage_rollups",
                "missing_token_count",
                "INTEGER NOT NULL DEFAULT 0",
            )
            await self.db.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', '5') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            await self.db.commit()

    async def close(self) -> None:
        async with self._operation_lock:
            if self._db is not None:
                await self._db.close()
                self._db = None

    async def backup_to(self, destination: str) -> None:
        """Create an online SQLite backup under repository serialization."""
        async with self._operation():
            target = sqlite3.connect(destination)
            try:
                await self.db.backup(target)
            finally:
                target.close()

    async def _ensure_column(self, table: str, column: str, definition: str) -> None:
        cursor = await self.db.execute(f"PRAGMA table_info({table})")
        names = {str(row[1]) for row in await cursor.fetchall()}
        if column not in names:
            await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AccountRepository]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("transaction requires an asyncio task")
        if self._transaction_owner is task:
            raise RuntimeError("nested repository transactions are not supported")
        async with self._operation_lock:
            self._transaction_owner = task
            try:
                await self.db.execute("BEGIN IMMEDIATE")
                yield self
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
            finally:
                self._transaction_owner = None

    @asynccontextmanager
    async def _operation(
        self, *, write: bool = False
    ) -> AsyncIterator[aiosqlite.Connection]:
        if self._transaction_owner is asyncio.current_task():
            yield self.db
            return
        async with self._operation_lock:
            try:
                yield self.db
                if write:
                    await self.db.commit()
            except BaseException:
                if write:
                    await self.db.rollback()
                raise

    @staticmethod
    async def _configure_connection(connection: aiosqlite.Connection) -> None:
        await connection.execute("PRAGMA journal_mode=WAL")
        await connection.execute("PRAGMA foreign_keys=ON")
        await connection.execute("PRAGMA busy_timeout=5000")
        await connection.execute("PRAGMA synchronous=NORMAL")
