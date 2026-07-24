"""Per-account token, quota, points, and check-in metric snapshots."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from qb2api.accounts.models import Credential
from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.config import Settings

from .quota import QoderQuotaClient, QuotaUnavailableError, normalize_quota

logger = logging.getLogger("qb2api.checkin.metrics")


class MetricsScheduler:
    """Single-flight scheduler with bounded per-account retry backoff."""

    def __init__(
        self,
        *,
        settings: Settings,
        repo: AccountRepository,
        registry: AccountRegistry,
        resolver: CredentialResolver,
        qoder_quota: QoderQuotaClient | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.registry = registry
        self.resolver = resolver
        self.qoder_quota = qoder_quota or QoderQuotaClient(
            base_url=settings.qoder_checkin_base,
            path=settings.qoder_quota_path,
            timeout=float(settings.checkin_request_timeout_seconds),
        )
        self._task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[dict[str, Any]] | None = None
        self._lock = asyncio.Lock()
        self._backoff: dict[str, tuple[int, datetime]] = {}
        self._closed = False
        self._enabled = settings.metrics_enabled
        self._wakeup = asyncio.Event()
        self._last_refresh_at: datetime | None = None
        self._last_error: str | None = None
        self._last_result: dict[str, Any] | None = None

    def start(self) -> None:
        if self._task is None and self._enabled and not self._closed:
            self._task = asyncio.create_task(self._run(), name="qb2api-metrics")

    async def stop(self) -> None:
        self._closed = True
        self._wakeup.set()
        for task in (self._task, self._refresh_task):
            if task is not None and not task.done():
                task.cancel()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
        if self._refresh_task is not None:
            await asyncio.gather(self._refresh_task, return_exceptions=True)
        await self.qoder_quota.aclose()
        self._task = None
        self._refresh_task = None

    async def reconfigure(self, *, enabled: bool | None = None) -> None:
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("metrics enabled must be boolean")
        self._enabled = self.settings.metrics_enabled if enabled is None else enabled
        self._wakeup.set()
        if not self._enabled and self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif self._enabled:
            self.start()

    def status_snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self._enabled,
            "running": self._task is not None and not self._task.done(),
            "refresh_in_progress": self._refresh_task is not None and not self._refresh_task.done(),
            "last_refresh_at": self._last_refresh_at.isoformat() if self._last_refresh_at else None,
            "last_error": self._last_error,
            "last_result": self._last_result,
            "backoff": [
                {"metric": key, "attempts": attempts, "retry_at": retry_at.isoformat()}
                for key, (attempts, retry_at) in sorted(self._backoff.items())
            ],
        }

    async def refresh_once(self) -> dict[str, Any]:
        async with self._lock:
            if self._refresh_task is None or self._refresh_task.done():
                self._refresh_task = asyncio.create_task(self._refresh(), name="qb2api-metrics-refresh")
            task = self._refresh_task
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._last_error = type(error).__name__
            raise
        self._last_refresh_at = datetime.now(UTC)
        self._last_error = None
        self._last_result = result
        return result

    async def _run(self) -> None:
        while not self._closed and self._enabled:
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("metric refresh failed")
            self._wakeup.clear()
            try:
                await asyncio.wait_for(
                    self._wakeup.wait(),
                    timeout=max(30, self.settings.metrics_interval_seconds),
                )
            except TimeoutError:
                pass

    async def _refresh(self) -> dict[str, Any]:
        await self.registry.rebuild()
        metadata = await self.repo.list_credential_metadata()
        previous = {
            (row["provider"], row["account_id"], row["metric_kind"]): row
            for row in await self.repo.list_metric_snapshots()
        }
        counts = {"fresh": 0, "stale": 0, "unknown": 0, "unavailable": 0, "skipped": 0}
        seen: set[tuple[str, str, str]] = set()
        for item in metadata:
            provider = str(item["provider"])
            account_id = str(item["account_id"])
            purpose = str(item["purpose"])
            token_key = (provider, account_id, f"token:{purpose}")
            seen.add(token_key)
            status = _token_status(item.get("expires_at"))
            await self._write(
                token_key,
                {"status": status, "expires_at": item.get("expires_at")},
                status,
                previous,
                counts,
            )
            checkin_key = (provider, account_id, "checkin")
            seen.add(checkin_key)
            await self._checkin_snapshot(provider, account_id, checkin_key, previous, counts)
            if provider == "qoder" and purpose == "checkin":
                quota_key = (provider, account_id, "quota")
                seen.add(quota_key)
                await self._quota_snapshot(account_id, quota_key, previous, counts)
            elif provider == "codebuddy" and purpose == "checkin":
                points_key = (provider, account_id, "points")
                seen.add(points_key)
                await self._write(
                    points_key,
                    None,
                    "unknown",
                    previous,
                    counts,
                    "protocol_not_verified",
                )
        for row in previous.values():
            key = (row["provider"], row["account_id"], row["metric_kind"])
            if key not in seen:
                counts["skipped"] += 1
        return counts

    async def _checkin_snapshot(
        self,
        provider: str,
        account_id: str,
        key: tuple[str, str, str],
        previous: dict,
        counts: dict[str, int],
    ) -> None:
        try:
            local_date = datetime.now(ZoneInfo(self.settings.checkin_timezone)).date().isoformat()
            state = await self.repo.get_checkin_daily_state(
                provider,
                account_id,
                local_date,
                self.settings.checkin_timezone,
            )
        except Exception as error:
            await self._write(key, None, "unavailable", previous, counts, type(error).__name__)
            return
        value = {"local_date": local_date, "terminal_outcome": state.get("terminal_outcome") if state else None}
        status = "fresh" if state else "unknown"
        await self._write(key, value, status, previous, counts)

    async def _quota_snapshot(
        self,
        account_id: str,
        key: tuple[str, str, str],
        previous: dict,
        counts: dict[str, int],
    ) -> None:
        retry_at = self._backoff.get(
            self._backoff_key(key),
            (0, datetime.min.replace(tzinfo=UTC)),
        )[1]
        if datetime.now(UTC) < retry_at:
            prior = previous.get(key)
            if prior and prior.get("value") is not None:
                await self._write(
                    key,
                    prior["value"],
                    "stale",
                    previous,
                    counts,
                    "backoff",
                    observed_at=prior.get("observed_at"),
                )
            else:
                counts["skipped"] += 1
            return
        try:
            credential = await self.resolver.credential("qoder", account_id, "checkin")
            token = _access_token(credential)
            value = normalize_quota(await self.qoder_quota.fetch(token))
            await self._write(key, value, "fresh", previous, counts)
            self._backoff.pop(self._backoff_key(key), None)
        except (LookupError, QuotaUnavailableError) as error:
            await self._failure(key, previous, counts, str(error))
        except Exception as error:
            await self._failure(key, previous, counts, type(error).__name__)

    async def _failure(
        self,
        key: tuple[str, str, str],
        previous: dict,
        counts: dict[str, int],
        error: str,
    ) -> None:
        prior = previous.get(key)
        if prior and prior.get("value") is not None:
            status = "stale"
            value = prior["value"]
            observed_at = prior.get("observed_at")
        else:
            status = "unavailable"
            value = None
            observed_at = None
        await self._write(key, value, status, previous, counts, error, observed_at=observed_at)
        self._record_backoff(key)

    async def _write(
        self,
        key: tuple[str, str, str],
        value: Any,
        status: str,
        previous: dict,
        counts: dict[str, int],
        error: str | None = None,
        *,
        observed_at: str | None = None,
    ) -> None:
        await self.repo.upsert_metric_snapshot(
            provider=key[0],
            account_id=key[1],
            metric_kind=key[2],
            value=value,
            status=status,
            last_error=error,
            observed_at=observed_at,
        )
        counts[status] = counts.get(status, 0) + 1

    def _record_backoff(self, key: tuple[str, str, str]) -> None:
        marker = self._backoff_key(key)
        attempts = self._backoff.get(marker, (0, datetime.now(UTC)))[0] + 1
        delay = min(3600, 30 * (2 ** min(attempts - 1, 6)))
        self._backoff[marker] = (attempts, datetime.now(UTC) + timedelta(seconds=delay))

    @staticmethod
    def _backoff_key(key: tuple[str, str, str]) -> str:
        return ":".join(key)


def _access_token(credential: Credential) -> str:
    payload = credential.payload
    token = payload.get("access_token") or payload.get("device_token") or payload.get("token")
    if not isinstance(token, str) or not token.strip():
        raise QuotaUnavailableError("access token unavailable")
    return token.strip()


def _token_status(expires_at: str | None) -> str:
    if not expires_at:
        return "unknown"
    value = str(expires_at).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return "unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return "expired" if parsed <= datetime.now(UTC) else "valid"
