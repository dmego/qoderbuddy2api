"""Control Plane runtime ownership for persistence and schedulers."""

from __future__ import annotations

import asyncio
from typing import Any

from .accounts import AccountRegistry, AccountRepository, CredentialResolver, CredentialVault
from .admin.auth import LoginRateLimiter
from .admin.backup import BackupService
from .admin.sessions import AdminSessionStore
from .auth.codebuddy_oauth import CodeBuddyOAuthClient
from .auth.flows import FlowStore
from .checkin.growth_automation import GrowthAutomation
from .checkin.growth_scheduler import GrowthScheduler
from .checkin.metrics import MetricsScheduler
from .checkin.scheduler import CheckinScheduler
from .checkin.service import CheckinService
from .config import Settings
from .control.model_sync_scheduler import ModelSyncScheduler
from .control.telemetry import UsageRollupService
from .storage_permissions import ensure_private_directory


class RuntimeServices:
    """Own Control Plane services and close every resource exactly once."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.account_repo: AccountRepository | None = None
        self.credential_vault: CredentialVault | None = None
        self.account_registry: AccountRegistry | None = None
        self.credential_resolver: CredentialResolver | None = None
        self.checkin_service: CheckinService | None = None
        self.checkin_scheduler: CheckinScheduler | None = None
        self.growth_scheduler: GrowthScheduler | None = None
        self.metrics_scheduler: MetricsScheduler | None = None
        self.model_sync_scheduler: ModelSyncScheduler | None = None
        self.backup_service: BackupService | None = None
        self.usage_rollup_service: UsageRollupService | None = None
        self.metrics_refresh_tasks: set[asyncio.Task[Any]] = set()
        self.admin_sessions = AdminSessionStore(
            ttl_hours=settings.admin_session_ttl_hours,
            idle_minutes=settings.admin_session_idle_minutes,
        )
        self.login_limiter = LoginRateLimiter()
        self.oauth_flows = FlowStore()
        self.codebuddy_oauth = CodeBuddyOAuthClient(
            base_url=settings.codebuddy_endpoint,
            timeout=float(settings.codebuddy_oauth_timeout),
        )
        self._closed = False

    @classmethod
    async def start(cls, settings: Settings) -> RuntimeServices:
        settings.validate_startup()
        runtime = cls(settings)
        try:
            if settings.credential_key:
                await runtime._start_durable()
        except BaseException:
            await runtime.close()
            raise
        return runtime

    async def _start_durable(self) -> None:
        vault = CredentialVault(self.settings.credential_key or "")
        repository = AccountRepository(self._database_path())
        await repository.connect()
        self.account_repo = repository
        await repository.migrate()
        await repository.recover_metric_refresh_operations()
        registry = AccountRegistry(
            repository,
            vault,
            codebuddy_tokens=self.settings.codebuddy_tokens or [],
            qoder_tokens=self.settings.qoder_tokens or [],
        )
        resolver = CredentialResolver(
            repository,
            vault,
            registry,
            skew_seconds=self.settings.codebuddy_oauth_refresh_skew,
        )
        self.credential_vault = vault
        self.account_registry = registry
        self.credential_resolver = resolver
        self.backup_service = BackupService(
            data_dir=self.settings.data_dir,
            repository=repository,
        )
        self.usage_rollup_service = UsageRollupService(
            settings=self.settings,
            repository=repository,
        )
        await self._load_runtime_settings(repository)
        self.admin_sessions = AdminSessionStore(
            repository,
            ttl_hours=self.settings.admin_session_ttl_hours,
            idle_minutes=self.settings.admin_session_idle_minutes,
        )
        await self.admin_sessions.revoke_all()
        await registry.rebuild()
        self._start_checkin_services()
        self._start_metrics_services()
        self._start_growth_services()
        self._start_model_sync_services()
        self.usage_rollup_service.start()

    async def _load_runtime_settings(self, repository: AccountRepository) -> None:
        from .control.settings import SettingsApplier

        for item in await repository.list_runtime_settings():
            key, value = item["key"], item["value"]
            try:
                SettingsApplier.validate(key, value)
                setattr(self.settings, SettingsApplier.attribute(key), value)
                await repository.update_runtime_setting_status(key, status="effective")
            except (TypeError, ValueError) as error:
                await repository.update_runtime_setting_status(
                    key,
                    status="failed",
                    last_error=type(error).__name__,
                )

    def _start_checkin_services(self) -> None:
        if not all(
            (self.account_repo, self.account_registry, self.credential_resolver, self.credential_vault)
        ):
            return
        self.checkin_service = CheckinService(
            settings=self.settings,
            repo=self.account_repo,
            registry=self.account_registry,
            resolver=self.credential_resolver,
            vault=self.credential_vault,
        )
        self.checkin_scheduler = CheckinScheduler(self.checkin_service, self.settings)
        self.checkin_scheduler.start()

    def _start_metrics_services(self) -> None:
        if not all((self.account_repo, self.account_registry, self.credential_resolver)):
            return
        self.metrics_scheduler = MetricsScheduler(
            settings=self.settings,
            repo=self.account_repo,
            registry=self.account_registry,
            resolver=self.credential_resolver,
        )
        if self.checkin_service is not None:
            self.checkin_service.set_metrics_refresher(self.metrics_scheduler.refresh_once)
        self.metrics_scheduler.start()

    def _start_growth_services(self) -> None:
        if not all(
            (self.account_repo, self.account_registry, self.credential_resolver)
        ):
            return
        self.growth_scheduler = GrowthScheduler(
            settings=self.settings,
            automation=GrowthAutomation(settings=self.settings, repository=self.account_repo),
            registry=self.account_registry,
            resolver=self.credential_resolver,
            repo=self.account_repo,
            metrics_refresh=self.metrics_scheduler.refresh_once if self.metrics_scheduler else None,
        )
        self.growth_scheduler.start()

    def _start_model_sync_services(self) -> None:
        if not all(
            (self.account_repo, self.account_registry, self.credential_resolver)
        ):
            return
        self.model_sync_scheduler = ModelSyncScheduler(
            settings=self.settings,
            repo=self.account_repo,
            registry=self.account_registry,
            resolver=self.credential_resolver,
        )
        self.model_sync_scheduler.start()

    async def refresh_accounts(self) -> None:
        if self.account_registry is not None:
            await self.account_registry.rebuild()

    def attach(self, app: Any) -> None:
        app.state.runtime = self
        for name in (
            "settings", "account_repo", "credential_vault", "account_registry",
            "credential_resolver", "admin_sessions", "login_limiter", "oauth_flows",
            "codebuddy_oauth", "checkin_service", "checkin_scheduler", "growth_scheduler",
            "metrics_scheduler", "model_sync_scheduler",
            "backup_service", "usage_rollup_service",
        ):
            setattr(app.state, name, getattr(self, name))
        app.state.metrics_refresh_tasks = self.metrics_refresh_tasks
        app.state.refresh_provider_pools = self.refresh_accounts

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.checkin_scheduler is not None:
            await self.checkin_scheduler.stop()
        if self.growth_scheduler is not None:
            await self.growth_scheduler.close()
        if self.model_sync_scheduler is not None:
            await self.model_sync_scheduler.stop()
        await self._cancel_metric_refresh_tasks()
        if self.metrics_scheduler is not None:
            await self.metrics_scheduler.stop()
        if self.usage_rollup_service is not None:
            await self.usage_rollup_service.stop()
        if self.checkin_service is not None:
            await self.checkin_service.close()
        await self.codebuddy_oauth.aclose()
        if self.account_repo is not None:
            await self.account_repo.close()

    async def _cancel_metric_refresh_tasks(self) -> None:
        tasks = list(self.metrics_refresh_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.metrics_refresh_tasks.clear()

    async def apply_setting(self, key: str, value: Any) -> str:
        from .control.settings import SettingsApplier

        return await SettingsApplier(self.settings, self).apply(key, value)

    def validate_setting(self, key: str, value: Any) -> None:
        from .control.settings import SettingsApplier

        SettingsApplier.validate(key, value)

    def _database_path(self) -> str:
        data_dir = ensure_private_directory(self.settings.data_dir)
        return str(data_dir / "qb2api.sqlite3")
