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
                has_refresh_token=bool(payload.get("refresh_token")),
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
        return Credential(
            provider=provider,
            account_id=account_id,
            purpose=purpose,
            mode=current.mode,
            payload=payload,
            credential_version=version,
            expires_at=expires_at,
            has_refresh_token=bool(payload.get("refresh_token")),
        )


def _expires_at(expires_in: int | None) -> str | None:
    if not isinstance(expires_in, int) or expires_in <= 0:
        return None
    moment = datetime.now(UTC) + timedelta(seconds=expires_in)
    return moment.replace(microsecond=0).isoformat()
