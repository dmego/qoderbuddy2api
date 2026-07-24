"""Metric collection helpers kept separate from the scheduler lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from qb2api.accounts.models import Credential
from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository
from qb2api.accounts.resolver import CredentialResolver
from qb2api.config import Settings

from .quota import QuotaUnavailableError, normalize_quota

MetricKey = tuple[str, str, str]
MetricRows = dict[MetricKey, dict[str, Any]]


@dataclass(frozen=True)
class MetricDependencies:
    settings: Settings
    repo: AccountRepository
    registry: AccountRegistry
    resolver: CredentialResolver
    qoder_quota: Any


@dataclass
class MetricCollectionState:
    previous: MetricRows
    counts: dict[str, int]
    seen: set[MetricKey]


class MetricSnapshotCollector:
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
        for item in await self._dependencies.repo.list_credential_metadata():
            await self._collect_item(item, state)
        self._count_unseen_previous(state)
        return state.counts

    async def _previous_rows(self) -> MetricRows:
        snapshots = await self._dependencies.repo.list_metric_snapshots()
        return {
            (row["provider"], row["account_id"], row["metric_kind"]): row
            for row in snapshots
        }

    async def _collect_item(self, item: dict[str, Any], state: MetricCollectionState) -> None:
        provider = str(item["provider"])
        account_id = str(item["account_id"])
        purpose = str(item["purpose"])
        await self._write_token_snapshot(provider, account_id, purpose, item, state)
        await self._write_checkin_snapshot(provider, account_id, state)
        await self._write_provider_snapshot(provider, account_id, purpose, state)

    async def _write_token_snapshot(
        self,
        provider: str,
        account_id: str,
        purpose: str,
        item: dict[str, Any],
        state: MetricCollectionState,
    ) -> None:
        key = (provider, account_id, f"token:{purpose}")
        state.seen.add(key)
        status = _token_status(item.get("expires_at"))
        await self._write(
            key=key,
            value={"status": status, "expires_at": item.get("expires_at")},
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

    async def _write_provider_snapshot(
        self,
        provider: str,
        account_id: str,
        purpose: str,
        state: MetricCollectionState,
    ) -> None:
        if provider == "qoder" and purpose == "checkin":
            await self._write_quota_snapshot(account_id, state)
        elif provider == "codebuddy" and purpose == "checkin":
            key = (provider, account_id, "points")
            state.seen.add(key)
            await self._write(
                key=key,
                value=None,
                status="unknown",
                state=state,
                error="protocol_not_verified",
            )

    async def _write_quota_snapshot(
        self,
        account_id: str,
        state: MetricCollectionState,
    ) -> None:
        key = ("qoder", account_id, "quota")
        state.seen.add(key)
        if await self._write_backoff_snapshot(key, state):
            return
        try:
            credential = await self._dependencies.resolver.credential("qoder", account_id, "checkin")
            token = _access_token(credential)
            value = normalize_quota(await self._dependencies.qoder_quota.fetch(token))
        except (LookupError, QuotaUnavailableError) as error:
            await self._write_failure(key, state, str(error))
        except Exception as error:
            await self._write_failure(key, state, type(error).__name__)
        else:
            await self._write(key=key, value=value, status="fresh", state=state)
            self._backoff.pop(self._backoff_key(key), None)

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
        state.counts[status] = state.counts.get(status, 0) + 1

    def _local_date(self) -> str:
        timezone = self._dependencies.settings.checkin_timezone
        return datetime.now(ZoneInfo(timezone)).date().isoformat()

    def _count_unseen_previous(self, state: MetricCollectionState) -> None:
        for key in state.previous:
            if key not in state.seen:
                state.counts["skipped"] += 1

    def _record_backoff(self, key: MetricKey) -> None:
        marker = self._backoff_key(key)
        attempts = self._backoff.get(marker, (0, datetime.now(UTC)))[0] + 1
        delay = min(3600, 30 * (2 ** min(attempts - 1, 6)))
        self._backoff[marker] = (attempts, datetime.now(UTC) + timedelta(seconds=delay))

    @staticmethod
    def _backoff_key(key: MetricKey) -> str:
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
