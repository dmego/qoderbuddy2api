"""Metric collection helpers kept separate from the scheduler lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.config import Settings

from .metrics_providers import ProviderMetricCollectorMixin

MetricKey = tuple[str, str, str]
MetricRows = dict[MetricKey, dict[str, Any]]


@dataclass(frozen=True)
class MetricDependencies:
    settings: Settings
    repo: AccountRepository
    registry: AccountRegistry
    resolver: CredentialResolver
    qoder_quota: Any
    codebuddy_credits: Any
    qoder_activity: Any = None


@dataclass
class MetricCollectionState:
    previous: MetricRows
    counts: dict[str, int]
    seen: set[MetricKey]


class MetricSnapshotCollector(ProviderMetricCollectorMixin):
    """Collect persisted snapshots while retaining retry state across runs."""

    def __init__(self, dependencies: MetricDependencies, backoff: dict[str, tuple[int, datetime]]) -> None:
        self._dependencies = dependencies
        self._backoff = backoff

    async def collect(self) -> dict[str, int]:
        await self._dependencies.registry.rebuild()
        state = MetricCollectionState(
            previous=await self._previous_rows(),
            counts={"fresh": 0, "stale": 0, "unknown": 0, "unavailable": 0, "skipped": 0},
            seen=set(),
        )
        for account in await self._dependencies.repo.list_accounts():
            if not account.get("enabled"):
                continue
            provider = str(account["provider"])
            account_id = str(account["account_id"])
            for purpose in await self._dependencies.repo.list_purposes(provider, account_id):
                if not purpose.get("enabled"):
                    continue
                await self._collect_item(
                    provider=provider,
                    account_id=account_id,
                    purpose=str(purpose["purpose"]),
                    expires_at=purpose.get("expires_at"),
                    state=state,
                )
        self._count_unseen_previous(state)
        await self._prune_history()
        return state.counts

    async def _previous_rows(self) -> MetricRows:
        snapshots = await self._dependencies.repo.list_metric_snapshots()
        return {
            (row["provider"], row["account_id"], row["metric_kind"]): row
            for row in snapshots
        }

    async def _collect_item(
        self,
        *,
        provider: str,
        account_id: str,
        purpose: str,
        expires_at: str | None,
        state: MetricCollectionState,
    ) -> None:
        await self._write_token_snapshot(
            provider=provider,
            account_id=account_id,
            purpose=purpose,
            expires_at=expires_at,
            state=state,
        )
        await self._write_checkin_snapshot(provider, account_id, state)
        await self._write_provider_snapshot(
            provider=provider,
            account_id=account_id,
            purpose=purpose,
            state=state,
        )

    async def _write_token_snapshot(
        self,
        *,
        provider: str,
        account_id: str,
        purpose: str,
        expires_at: str | None,
        state: MetricCollectionState,
    ) -> None:
        key = (provider, account_id, f"token:{purpose}")
        state.seen.add(key)
        status = _token_status(expires_at)
        await self._write(
            key=key,
            value={"status": status, "expires_at": expires_at},
            status=status,
            state=state,
        )

    async def _write_checkin_snapshot(
        self,
        provider: str,
        account_id: str,
        state: MetricCollectionState,
    ) -> None:
        key = (provider, account_id, "checkin")
        state.seen.add(key)
        try:
            local_date = self._local_date()
            daily_state = await self._dependencies.repo.get_checkin_daily_state(
                provider,
                account_id,
                local_date,
                self._dependencies.settings.checkin_timezone,
            )
        except Exception as error:
            await self._write(
                key=key,
                value=None,
                status="unavailable",
                state=state,
                error=type(error).__name__,
            )
            return
        await self._write(
            key=key,
            value={
                "local_date": local_date,
                "terminal_outcome": daily_state.get("terminal_outcome") if daily_state else None,
            },
            status="fresh" if daily_state else "unknown",
            state=state,
        )

    async def _write_backoff_snapshot(
        self,
        key: MetricKey,
        state: MetricCollectionState,
    ) -> bool:
        retry_at = self._backoff.get(
            self._backoff_key(key),
            (0, datetime.min.replace(tzinfo=UTC)),
        )[1]
        if datetime.now(UTC) >= retry_at:
            return False
        prior = state.previous.get(key)
        if prior and prior.get("value") is not None:
            await self._write(
                key=key,
                value=prior["value"],
                status="stale",
                state=state,
                error="backoff",
                observed_at=prior.get("observed_at"),
            )
        else:
            state.counts["skipped"] += 1
        return True

    async def _write_failure(
        self,
        key: MetricKey,
        state: MetricCollectionState,
        error: str,
    ) -> None:
        prior = state.previous.get(key)
        if prior and prior.get("value") is not None:
            status, value, observed_at = "stale", prior["value"], prior.get("observed_at")
        else:
            status, value, observed_at = "unavailable", None, None
        await self._write(
            key=key,
            value=value,
            status=status,
            state=state,
            error=error,
            observed_at=observed_at,
        )
        self._record_backoff(key)

    async def _write(
        self,
        *,
        key: MetricKey,
        value: Any,
        status: str,
        state: MetricCollectionState,
        error: str | None = None,
        observed_at: str | None = None,
    ) -> None:
        await self._dependencies.repo.upsert_metric_snapshot(
            provider=key[0],
            account_id=key[1],
            metric_kind=key[2],
            value=value,
            status=status,
            last_error=error,
            observed_at=observed_at,
        )
        if value is not None:
            await self._dependencies.repo.upsert_metric_history(
                provider=key[0],
                account_id=key[1],
                metric_kind=key[2],
                value=value,
                status=status,
                observed_at=observed_at,
            )
        state.counts[status] = state.counts.get(status, 0) + 1

    def _local_date(self) -> str:
        timezone = self._dependencies.settings.checkin_timezone
        return datetime.now(ZoneInfo(timezone)).date().isoformat()

    def _count_unseen_previous(self, state: MetricCollectionState) -> None:
        for key in state.previous:
            if key not in state.seen:
                state.counts["skipped"] += 1

    async def _prune_history(self) -> None:
        retention = self._dependencies.settings.metrics_history_retention_days
        if retention <= 0:
            return
        before = (datetime.now(UTC) - timedelta(days=retention)).isoformat()
        await self._dependencies.repo.delete_metric_history_before(before)

    def _record_backoff(self, key: MetricKey) -> None:
        marker = self._backoff_key(key)
        attempts = self._backoff.get(marker, (0, datetime.now(UTC)))[0] + 1
        delay = min(3600, 30 * (2 ** min(attempts - 1, 6)))
        self._backoff[marker] = (attempts, datetime.now(UTC) + timedelta(seconds=delay))

    @staticmethod
    def _backoff_key(key: MetricKey) -> str:
        return ":".join(key)


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
