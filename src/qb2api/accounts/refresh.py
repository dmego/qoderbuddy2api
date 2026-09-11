"""Best-effort bearer rotation for accounts the Control Plane can refresh.

Two upstream contracts are wired up:

* WorkBuddy international (``POST /v2/plugin/auth/token/refresh`` with
  ``X-Refresh-Token``) — the stored refresh token is the credential.
* OrcaTerm (``OAuthRefreshToken``) — the console takes the *access* token as
  the refresh credential and issues a fresh 2h token for the same identity.
  This matters because OrcaTerm access tokens live only two hours, so without
  rotation every account would need a manual re-login twice a day.

Everything else falls back to "keep serving the stored credential until it
actually expires", which is the historical behaviour — the resolver keeps the
loaded credential when the callback returns ``None``.

Raw tokens never reach logs, audit records, or SQLite payload columns beyond the
encrypted credential blob they already live in.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ..auth.orcaterm import OrcaTermAuthClient
from ..auth.workbuddy_intl import WorkBuddyIntlAuthClient
from .models import Credential
from .repo_credentials import CredentialVersionConflict
from .repository import AccountRepository
from .vault import CredentialVault

logger = logging.getLogger("qb2api.accounts.refresh")

REFRESHABLE_PROVIDERS = frozenset({"workbuddy_intl", "orcaterm"})


def _carries_refresh_token(provider: str, payload: dict[str, Any]) -> bool:
    """Whether the row advertises a usable refresh path to the operator.

    ``has_refresh_token`` is shown in the admin credential table, so it must
    mean "this credential can be renewed" rather than literally "a field named
    refresh_token exists". OrcaTerm renews with the access token itself, so it
    has no such field yet is fully refreshable.
    """
    if provider in REFRESHABLE_PROVIDERS:
        return True
    return bool(payload.get("refresh_token"))


class BearerRefreshExecutor:
    """Refresh stored bearer credentials for providers with a usable refresh API."""

    def __init__(
        self,
        *,
        repository: AccountRepository,
        vault: CredentialVault,
        intl_client: WorkBuddyIntlAuthClient,
        orcaterm_client: OrcaTermAuthClient | None = None,
    ) -> None:
        self._repo = repository
        self._vault = vault
        self._intl = intl_client
        self._orcaterm = orcaterm_client

    async def __call__(self, credential: Credential) -> Credential | None:
        if credential.provider not in REFRESHABLE_PROVIDERS:
            return None
        if credential.provider == "orcaterm":
            return await self._refresh_orcaterm(credential)
        return await self._refresh_intl(credential)

    async def _refresh_intl(self, credential: Credential) -> Credential | None:
        refresh_token = credential.payload.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            return None
        result = await self._intl.refresh(refresh_token.strip())
        if result.status != "success" or not result.access_token:
            logger.warning(
                "credential refresh failed for %s/%s/%s: %s",
                credential.provider,
                credential.account_id,
                credential.purpose,
                result.message or result.status,
            )
            return None
        return await self._persist(credential, result)

    async def _refresh_orcaterm(self, credential: Credential) -> Credential | None:
        """Rotate an OrcaTerm access token with itself as the refresh credential."""
        if self._orcaterm is None:
            return None
        access_token = credential.payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            return None
        result = await self._orcaterm.refresh(access_token.strip())
        if result.status != "success" or not result.access_token:
            # Once the token has actually expired the console answers
            # TOKEN_EXPIRED and only a fresh browser login can recover, so this
            # is expected rather than an error worth retrying in a loop.
            logger.warning(
                "credential refresh failed for %s/%s/%s: %s",
                credential.provider,
                credential.account_id,
                credential.purpose,
                result.message or result.status,
            )
            return None
        return await self._persist(credential, result)

    async def _persist(
        self,
        current: Credential,
        result: Any,
    ) -> Credential | None:
        provider = current.provider
        account_id = current.account_id
        purpose = current.purpose
        payload = dict(current.payload)
        payload["access_token"] = result.access_token
        # Upstream keeps the same refresh token value; only overwrite when a new
        # one is actually issued. OrcaTerm has no separate refresh token at all.
        issued_refresh = getattr(result, "refresh_token", None)
        if issued_refresh:
            payload["refresh_token"] = issued_refresh
        expires_at = _expires_at(result.expires_in)
        try:
            version = await self._repo.upsert_credential(
                provider=provider,
                account_id=account_id,
                purpose=purpose,
                mode=current.mode,
                encrypted_payload=self._vault.encrypt(payload),
                has_refresh_token=_carries_refresh_token(provider, payload),
                expires_at=expires_at,
                expected_version=current.credential_version,
            )
        except CredentialVersionConflict:
            # Another task rotated it first; reload instead of overwriting.
            logger.info("credential refresh raced for %s/%s/%s", provider, account_id, purpose)
            return None
        except Exception as error:
            logger.warning("credential refresh persist failed: %s", type(error).__name__)
            return None
        await self._sync_purpose_expiry(provider, account_id, purpose, expires_at)
        return Credential(
            provider=provider,
            account_id=account_id,
            purpose=purpose,
            mode=current.mode,
            payload=payload,
            credential_version=version,
            expires_at=expires_at,
            has_refresh_token=_carries_refresh_token(provider, payload),
        )

    async def _sync_purpose_expiry(
        self,
        provider: str,
        account_id: str,
        purpose: str,
        expires_at: str | None,
    ) -> None:
        """Mirror the new deadline onto the account purpose row.

        The admin account view reads ``account_purposes.expires_at``, so a
        rotation that only touched the credential row would keep advertising the
        old deadline and make a healthy account look about to lapse. Failure
        here is cosmetic — the credential itself is already stored — so it must
        never turn a successful rotation into an error.
        """
        try:
            purposes = await self._repo.list_purposes(provider, account_id)
        except Exception:
            logger.warning("purpose lookup failed for %s/%s", provider, account_id)
            return
        current = next((item for item in purposes if item.get("purpose") == purpose), None)
        if current is None:
            return
        try:
            await self._repo.upsert_purpose(
                provider=provider,
                account_id=account_id,
                purpose=purpose,
                enabled=bool(current.get("enabled")),
                status=current.get("status") or "active",
                verification_status=current.get("verification_status") or "not_required",
                capabilities=current.get("capabilities"),
                verified_at=current.get("verified_at"),
                expires_at=expires_at,
                last_success_at=current.get("last_success_at"),
                failure_count=current.get("failure_count", 0),
                last_error=current.get("last_error"),
            )
        except Exception:
            logger.warning("purpose expiry sync failed for %s/%s/%s", provider, account_id, purpose)


def _expires_at(expires_in: int | None) -> str | None:
    if not isinstance(expires_in, int) or expires_in <= 0:
        return None
    moment = datetime.now(UTC) + timedelta(seconds=expires_in)
    return moment.replace(microsecond=0).isoformat()
