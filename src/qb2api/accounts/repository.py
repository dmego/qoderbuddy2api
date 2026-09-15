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
from .repo_growth import GrowthRepositoryMixin
from .repo_growth_log import GrowthLogMixin
from .repo_metric_history import MetricHistoryRepositoryMixin
from .repo_metric_refresh import MetricRefreshRepositoryMixin
from .repo_proxy_keys import ProxyKeyRepositoryMixin
from .repo_routes import AccountModelBlockRepositoryMixin, RoutePolicyRepositoryMixin
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
    GrowthRepositoryMixin,
    MetricHistoryRepositoryMixin,
    ControlRepositoryMixin,
    ProxyKeyRepositoryMixin,
    RoutePolicyRepositoryMixin,
    AccountModelBlockRepositoryMixin,
    CheckinRepositoryMixin,
    SessionRepositoryMixin,
    TelemetryRepositoryMixin,
    GrowthLogMixin,
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
            await self._ensure_column("request_events", "reasoning_effort", "TEXT")
            for column, definition in (
                ("reward_credits", "REAL"),
                ("reward_expires_at", "TEXT"),
                ("quota_before_json", "TEXT"),
                ("quota_after_json", "TEXT"),
                ("quota_delta_json", "TEXT"),
                ("quota_observed_at", "TEXT"),
                ("quota_change_status", "TEXT"),
            ):
                await self._ensure_column("checkin_attempts", column, definition)
            for column, definition in (
                ("confirmed", "TEXT"),
                ("confirmed_at", "TEXT"),
                ("confirm_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("run_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("official_score", "INTEGER"),
                ("official_streak_days", "INTEGER"),
                ("official_updated_at", "TEXT"),
                ("official_observed_at", "TEXT"),
            ):
                await self._ensure_column("workbuddy_active_days", column, definition)
            await self.db.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', '7') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            await self._backfill_refreshable_flags()
            history_changed = await self._backfill_metric_history_payloads()
            recovered = await self._recover_lost_and_found()
            await self.db.commit()
            if history_changed or recovered:
                await self._compact_database()

    async def _backfill_metric_history_payloads(self) -> bool:
        """Drop per-package detail that predates the bounded history payload.

        ``packages`` accounted for ~97% of the ``points`` bytes and the history
        trend only reads scalar totals, so rows written before the collector
        stopped recording it are pure dead weight (~120 MiB here). Returns
        whether anything changed, so the caller only pays for a VACUUM then.
        """
        cursor = await self.db.execute(
            """
            UPDATE account_metric_history
               SET metric_value_json = json_remove(metric_value_json, '$.packages')
             WHERE CASE WHEN json_valid(metric_value_json)
                        THEN json_type(metric_value_json, '$.packages') END = 'array'
            """
        )
        return cursor.rowcount > 0

    async def _compact_database(self) -> None:
        """Return freed pages to the filesystem.

        Must run outside a transaction, which is why the caller commits first.
        """
        await self.db.execute("VACUUM")

    async def _recover_lost_and_found(self) -> int:
        """Reclaim request events stranded by a past ``.recover`` and drop the remnant.

        A damaged page earlier in this database's life forced a recovery run
        that copied orphaned pages into SQLite's ``lost_and_found`` scratch
        table instead of back into their original tables. Those rows are intact
        request events (every NOT NULL column matches), so they are restored
        before the scratch table is removed. Guarded on the table's existence,
        so it is a no-op everywhere else and after the first run.
        """
        cursor = await self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='lost_and_found'"
        )
        if await cursor.fetchone() is None:
            return 0
        await self.db.execute("""
            INSERT OR IGNORE INTO request_events
                (event_id, request_id, provider, account_id, model_id, protocol,
                 status, http_status, input_tokens, output_tokens, latency_ms,
                 stream_committed, started_at, finished_at, error_code,
                 redacted_error, reasoning_effort)
            SELECT c0, c1, c2, c3, c4, c5, c6, c7, c8, c9, c10,
                   CASE WHEN c11 IN (0, 1) THEN c11 ELSE 0 END,
                   c12, c13, c14, c15, c16
              FROM lost_and_found
             WHERE nfield = 17
               AND typeof(c0) = 'text' AND typeof(c1) = 'text'
               AND typeof(c2) = 'text' AND typeof(c4) = 'text'
               AND typeof(c5) = 'text' AND typeof(c6) = 'text'
               AND typeof(c12) = 'text' AND c12 LIKE '____-__-__T%'
        """)
        recovered = max(0, cursor.rowcount)
        await self.db.execute("DROP TABLE lost_and_found")
        return recovered

    async def _backfill_refreshable_flags(self) -> None:
        """Correct rows whose ``has_refresh_token`` predates a refresh contract.

        The flag is derived, so it is always safe to reassert it: any provider in
        REFRESHABLE_PROVIDERS can be renewed regardless of whether its payload
        carries a literal ``refresh_token`` (OrcaTerm renews with the access
        token itself). Rows written before a provider gained a contract would
        otherwise keep claiming the credential cannot be renewed until the next
        rotation happened to rewrite them.
        """
        from .refresh import REFRESHABLE_PROVIDERS

        providers = sorted(REFRESHABLE_PROVIDERS)
        if not providers:
            return
        placeholders = ",".join("?" for _ in providers)
        await self.db.execute(
            f"UPDATE credentials SET has_refresh_token=1 "
            f"WHERE provider IN ({placeholders}) AND has_refresh_token=0",
            tuple(providers),
        )

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
