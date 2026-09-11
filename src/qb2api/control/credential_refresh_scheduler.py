"""Proactive rotation of short-lived credentials (Control-side only).

The resolver already rotates a credential on demand, but "on demand" only
happens while a snapshot is being built — and a snapshot is only built when the
Worker handshakes or is reloaded. An OrcaTerm token lives two hours, so a Worker
that stays up would keep presenting an expired token: the on-demand path never
gets a chance to run before the token dies.

This scheduler closes that gap. It scans the stored credentials, and for any
refreshable provider whose token is inside the lead window it forces a rotation
and then asks for a runtime refresh so the Worker picks the new value up. The
lead window is deliberately much wider than the scan interval, so a token is
always rotated well before it expires — once it has actually expired the console
answers ``TOKEN_EXPIRED`` and only a fresh browser login can recover.

Raw tokens never reach logs; failures are reported by provider and account only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from qb2api.accounts.refresh import REFRESHABLE_PROVIDERS

logger = logging.getLogger("qb2api.control.credential_refresh")

# Providers whose credentials are short-lived enough to need proactive rotation.
# The international WorkBuddy token lives about a year, so it is refreshed on
# demand and needs no scheduler attention.
SHORT_LIVED_PROVIDERS = frozenset({"orcaterm"})

_MIN_INTERVAL = 60


def _parse_expires_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class CredentialRefreshScheduler:
    """Rotate short-lived credentials ahead of expiry and publish the result."""

    def __init__(
        self,
        *,
        settings: Any,
        repo: Any,
        resolver: Any,
        refresh_callback: Any = None,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._resolver = resolver
        self._refresh_callback = refresh_callback
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()

    @property
    def interval_seconds(self) -> int:
        return max(_MIN_INTERVAL, int(self._settings.credential_refresh_interval_seconds))

    @property
    def lead_seconds(self) -> int:
        return max(0, int(self._settings.credential_refresh_lead_seconds))

    def set_refresh_callback(self, callback: Any) -> None:
        self._refresh_callback = callback

    def start(self) -> None:
        if not self._settings.credential_refresh_enabled:
            logger.info("credential refresh scheduler disabled")
            return
        if self._task is not None:
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._loop(), name="credential-refresh")
        logger.info(
            "credential refresh started (interval=%ds lead=%ds)",
            self.interval_seconds,
            self.lead_seconds,
        )

    async def stop(self) -> None:
        self._stopped.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await self.refresh_once()
            except Exception:
                logger.warning("credential refresh cycle failed", exc_info=True)
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=max(1.0, float(self.interval_seconds))
                )
                break
            except TimeoutError:
                pass

    async def refresh_once(self) -> int:
        """Rotate every credential inside the lead window; return how many moved."""
        rotated = 0
        for item in await self._due_credentials():
            provider = item["provider"]
            account_id = item["account_id"]
            purpose = item.get("purpose") or "chat"
            try:
                credential = await self._resolver.credential(
                    provider, account_id, purpose, force_refresh=True
                )
            except Exception:
                logger.warning(
                    "credential refresh raised for %s/%s/%s",
                    provider,
                    account_id,
                    purpose,
                    exc_info=True,
                )
                continue
            # The resolver falls back to the stored credential when rotation
            # fails, so a version change is the signal that it actually moved.
            if credential.credential_version != item.get("credential_version"):
                rotated += 1
                logger.info(
                    "rotated credential for %s/%s/%s (version %s -> %s)",
                    provider,
                    account_id,
                    purpose,
                    item.get("credential_version"),
                    credential.credential_version,
                )
        if rotated and self._refresh_callback is not None:
            await self._refresh_callback()
        return rotated

    async def _due_credentials(self) -> list[dict[str, Any]]:
        """Refreshable credentials whose token is within the lead window."""
        metadata = await self._repo.list_credential_metadata()
        now = datetime.now(UTC)
        due: list[dict[str, Any]] = []
        for item in metadata:
            provider = item.get("provider")
            if provider not in SHORT_LIVED_PROVIDERS:
                continue
            if provider not in REFRESHABLE_PROVIDERS:
                continue
            expires_at = _parse_expires_at(item.get("expires_at"))
            if expires_at is None:
                continue
            remaining = (expires_at - now).total_seconds()
            if remaining <= self.lead_seconds:
                due.append(item)
        return due
