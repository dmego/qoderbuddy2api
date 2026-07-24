"""Purpose-scoped credential resolution with versioned cache and single-flight refresh."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .models import Credential
from .registry import AccountRegistry
from .repository import AccountRepository
from .vault import CredentialVault

RefreshCallback = Callable[[str, str, str, Credential], Awaitable[Credential | None]]

CacheKey = tuple[str, str, str]  # provider, account_id, purpose


def _parse_expires_at(value: str | int | float | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return datetime.fromtimestamp(int(s), tz=UTC)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _now() -> datetime:
    return datetime.now(UTC)


class CredentialResolver:
    """Resolve credentials for (provider, account_id, purpose).

    - in-memory cache keyed by purpose with credential_version
    - single-flight refresh lock per key
    - skew-based expiry: now >= expires_at - skew triggers refresh
    """

    def __init__(
        self,
        repo: AccountRepository,
        vault: CredentialVault,
        registry: AccountRegistry | None = None,
        *,
        skew_seconds: int = 120,
        refresh_callback: RefreshCallback | None = None,
    ) -> None:
        self._repo = repo
        self._vault = vault
        self._registry = registry
        self._skew = max(0, int(skew_seconds))
        self._refresh_callback = refresh_callback
        self._cache: dict[CacheKey, Credential] = {}
        self._locks: dict[CacheKey, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def set_refresh_callback(self, cb: RefreshCallback | None) -> None:
        self._refresh_callback = cb

    def invalidate(
        self,
        provider: str | None = None,
        account_id: str | None = None,
        purpose: str | None = None,
    ) -> None:
        if provider is None and account_id is None and purpose is None:
            self._cache.clear()
            return
        drop = [
            k
            for k in self._cache
            if (provider is None or k[0] == provider)
            and (account_id is None or k[1] == account_id)
            and (purpose is None or k[2] == purpose)
        ]
        for k in drop:
            self._cache.pop(k, None)

    async def _lock_for(self, key: CacheKey) -> asyncio.Lock:
        async with self._global_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def needs_refresh(self, cred: Credential, now: datetime | None = None) -> bool:
        """True when access is within skew window of expires_at (or already past)."""
        exp = _parse_expires_at(cred.expires_at or cred.payload.get("expires_at"))
        if exp is None:
            return False
        return (now or _now()) >= exp - timedelta(seconds=self._skew)

    async def credential(
        self,
        provider: str,
        account_id: str,
        purpose: str = "chat",
        *,
        force_refresh: bool = False,
    ) -> Credential:
        key: CacheKey = (provider, account_id, purpose)
        lock = await self._lock_for(key)
        async with lock:
            cached = self._cache.get(key)
            if cached is not None and not force_refresh and not self.needs_refresh(cached):
                return cached

            # env static path (no DB row)
            if self._registry is not None and self._registry.is_env_account(provider, account_id):
                secret = self._registry.env_secret(provider, account_id, purpose)
                if secret is None:
                    raise LookupError(
                        f"env account unavailable or shadowed: {provider}/{account_id}"
                    )
                mode = "pat" if provider == "qoder" else "bearer"
                payload: dict[str, Any] = (
                    {"pat": secret} if provider == "qoder" else {"access_token": secret}
                )
                cred = Credential(
                    provider=provider,
                    account_id=account_id,
                    purpose=purpose,
                    mode=mode,
                    payload=payload,
                    credential_version=1,
                    expires_at=None,
                    has_refresh_token=False,
                )
                self._cache[key] = cred
                return cred

            loaded = await self._load_from_repo(provider, account_id, purpose)
            if force_refresh or self.needs_refresh(loaded):
                refreshed = await self._try_refresh(key, loaded)
                if refreshed is not None:
                    self._cache[key] = refreshed
                    return refreshed
            self._cache[key] = loaded
            return loaded

    async def _load_from_repo(
        self, provider: str, account_id: str, purpose: str
    ) -> Credential:
        row = await self._repo.get_credential(provider, account_id, purpose)
        # inherit_chat: checkin purpose may reuse chat credential material
        if row is None and purpose == "checkin":
            row = await self._repo.get_credential(provider, account_id, "chat")
            if row is not None:
                payload = self._vault.decrypt(row["encrypted_payload"])
                return Credential(
                    provider=provider,
                    account_id=account_id,
                    purpose="checkin",
                    mode="inherit_chat",
                    payload=payload,
                    credential_version=int(row.get("credential_version") or 1),
                    expires_at=row.get("expires_at"),
                    has_refresh_token=bool(row.get("has_refresh_token")),
                )
        if row is None:
            raise LookupError(f"no credential for {provider}/{account_id}/{purpose}")
        payload = self._vault.decrypt(row["encrypted_payload"])
        return Credential(
            provider=provider,
            account_id=account_id,
            purpose=purpose,
            mode=row.get("mode") or "bearer",
            payload=payload,
            credential_version=int(row.get("credential_version") or 1),
            expires_at=row.get("expires_at"),
            has_refresh_token=bool(row.get("has_refresh_token")),
        )

    async def _try_refresh(self, key: CacheKey, current: Credential) -> Credential | None:
        cb = self._refresh_callback
        if cb is None:
            return None
        return await cb(key[0], key[1], key[2], current)
