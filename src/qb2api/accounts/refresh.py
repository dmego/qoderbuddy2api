"""Best-effort bearer rotation for accounts the Control Plane can refresh.

Only the international WorkBuddy deployment exposes a refresh contract that the
Control Plane can call with the stored refresh token (``POST
/v2/plugin/auth/token/refresh`` with ``X-Refresh-Token``). Everything else falls
back to "keep serving the stored credential until it actually expires", which
is the historical behaviour — the resolver keeps the loaded credential when the
callback returns ``None``.

Raw tokens never reach logs, audit records, or SQLite payload columns beyond the
encrypted credential blob they already live in.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ..auth.workbuddy_intl import WorkBuddyIntlAuthClient
from .models import Credential
from .repo_credentials import CredentialVersionConflict
from .repository import AccountRepository
from .vault import CredentialVault

logger = logging.getLogger("qb2api.accounts.refresh")

REFRESHABLE_PROVIDERS = frozenset({"workbuddy_intl"})


class BearerRefreshExecutor:
    """Refresh stored bearer pairs for providers with a usable refresh API."""

    def __init__(
        self,
        *,
        repository: AccountRepository,
        vault: CredentialVault,
        intl_client: WorkBuddyIntlAuthClient,
    ) -> None:
        self._repo = repository
        self._vault = vault
        self._intl = intl_client

    async def __call__(self, credential: Credential) -> Credential | None:
        if credential.provider not in REFRESHABLE_PROVIDERS:
            return None
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
        # one is actually issued.
        if result.refresh_token:
            payload["refresh_token"] = result.refresh_token
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
