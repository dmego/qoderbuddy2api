"""Single-account check-in execution and purpose-scoped state changes."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from qb2api.accounts.registry import AccountRegistry
from qb2api.accounts.repository import AccountRepository, CredentialVersionConflict
from qb2api.accounts.resolver import CredentialResolver
from qb2api.accounts.vault import CredentialVault
from qb2api.config import Settings

from . import executor_helpers
from .codebuddy import WorkBuddyClient
from .models import SUCCESS_OUTCOMES, CheckInOutcome, CheckInResult, RefreshResult
from .qoder import QoderCheckinClient

logger = logging.getLogger("qb2api.checkin.executors")


class CheckinExecutor:
    def __init__(
        self,
        *,
        settings: Settings,
        repo: AccountRepository,
        registry: AccountRegistry,
        resolver: CredentialResolver,
        vault: CredentialVault,
        workbuddy: WorkBuddyClient | None = None,
        qoder: QoderCheckinClient | None = None,
    ) -> None:
        self._repo = repo
        self._registry = registry
        self._resolver = resolver
        self._vault = vault
        self._workbuddy = workbuddy or executor_helpers.workbuddy_client(settings)
        self._qoder = qoder or executor_helpers.qoder_client(settings)

    @property
    def qoder_client(self) -> QoderCheckinClient:
        return self._qoder

    async def close(self) -> None:
        await self._workbuddy.close()
        await self._qoder.close()

    async def run(self, provider: str, account_id: str) -> CheckInResult:
        if provider == "codebuddy":
            return await self._run_codebuddy(account_id)
        if provider == "qoder":
            return await self._run_qoder(account_id)
        return CheckInResult(
            outcome=CheckInOutcome.SKIPPED,
            provider=provider,
            account_id=account_id,
            message="unknown provider",
        )

    async def _run_codebuddy(self, account_id: str) -> CheckInResult:
        try:
            credential = await self._resolver.credential(
                "codebuddy", account_id, "checkin"
            )
        except LookupError:
            return executor_helpers.missing_credential("codebuddy", account_id)
        mode = credential.mode
        result = await self._workbuddy.checkin(
            account_id=account_id,
            auth_mode="bearer" if mode == "inherit_chat" else mode,
            access_token=(
                credential.payload.get("access_token")
                or credential.payload.get("token")
            ),
            cookie=credential.payload.get("cookie"),
        )
        if result.outcome in SUCCESS_OUTCOMES:
            await self._set_purpose(
                provider="codebuddy",
                account_id=account_id,
                status="active",
                verification_status="verified",
                success=True,
            )
        elif result.outcome == CheckInOutcome.NEEDS_REAUTH:
            await self._set_purpose(
                provider="codebuddy",
                account_id=account_id,
                status="needs_reauth",
                verification_status="rejected",
                last_error="auth_failed",
            )
        return result

    async def _run_qoder(self, account_id: str) -> CheckInResult:
        try:
            credential = await self._resolver.credential("qoder", account_id, "checkin")
        except LookupError:
            return executor_helpers.missing_credential("qoder", account_id)
        access_token = credential.payload.get("access_token") or credential.payload.get("token")
        if not access_token:
            return executor_helpers.needs_reauth(
                "qoder", account_id, "missing access_token"
            )
        result = await self._qoder.checkin(
            access_token=access_token,
            account_id=account_id,
        )
        record_reauth = True
        state_error = "auth_failed"
        refresh_token = credential.payload.get("refresh_token")
        if result.outcome == CheckInOutcome.NEEDS_REAUTH and refresh_token:
            result, record_reauth = await self._refresh_qoder(
                account_id,
                credential=credential,
                refresh_token=refresh_token,
            )
            state_error = "refresh_failed"
        await self._record_qoder_state(
            account_id,
            result,
            record_reauth=record_reauth,
            state_error=state_error,
        )
        return result

    async def _refresh_qoder(
        self,
        account_id: str,
        *,
        credential: Any,
        refresh_token: str,
    ) -> tuple[CheckInResult, bool]:
        refreshed = await self._qoder.refresh(
            refresh_token=refresh_token,
            account_id=account_id,
        )
        if not refreshed.ok or not refreshed.access_token:
            return await self._qoder_refresh_failure(
                account_id,
                credential=credential,
                refreshed=refreshed,
            )
        payload = {**credential.payload, "access_token": refreshed.access_token}
        if refreshed.refresh_token:
            payload["refresh_token"] = refreshed.refresh_token
        await self._commit_qoder_refresh(
            account_id,
            credential=credential,
            payload=payload,
        )
        latest = await self._resolver.credential("qoder", account_id, "checkin")
        access_token = latest.payload.get("access_token") or latest.payload.get("token")
        if not access_token:
            return (
                executor_helpers.needs_reauth(
                    "qoder", account_id, "missing refreshed access_token"
                ),
                True,
            )
        return (
            await self._qoder.checkin(
                access_token=access_token,
                account_id=account_id,
            ),
            True,
        )

    async def _commit_qoder_refresh(
        self,
        account_id: str,
        *,
        credential: Any,
        payload: dict[str, Any],
    ) -> None:
        try:
            await self._repo.upsert_credential(
                provider="qoder",
                account_id=account_id,
                purpose="checkin",
                mode=credential.mode or "access_refresh",
                encrypted_payload=self._vault.encrypt(payload),
                has_refresh_token=bool(payload.get("refresh_token")),
                expires_at=credential.expires_at,
                expected_version=credential.credential_version,
            )
        except CredentialVersionConflict:
            logger.info(
                "qoder refresh CAS lost for account %s; using winning credential",
                account_id,
            )
        finally:
            self._resolver.invalidate("qoder", account_id, "checkin")

    async def _qoder_refresh_failure(
        self,
        account_id: str,
        *,
        credential: Any,
        refreshed: RefreshResult,
    ) -> tuple[CheckInResult, bool]:
        outcome = refreshed.outcome or CheckInOutcome.FAILED
        result = CheckInResult(
            outcome=outcome,
            provider="qoder",
            account_id=account_id,
            http_status=refreshed.http_status,
            message=refreshed.message or "refresh failed",
        )
        if outcome not in {CheckInOutcome.AUTH_FAILED, CheckInOutcome.NEEDS_REAUTH}:
            return result, True
        self._resolver.invalidate("qoder", account_id, "checkin")
        try:
            latest = await self._resolver.credential("qoder", account_id, "checkin")
        except LookupError:
            return result, False
        if latest.credential_version == credential.credential_version:
            return result, True
        if latest.credential_version < credential.credential_version:
            return result, False
        access_token = latest.payload.get("access_token") or latest.payload.get("token")
        if not access_token:
            return result, False
        logger.info(
            "qoder stale refresh failed for account %s; retrying winning credential",
            account_id,
        )
        return (
            await self._qoder.checkin(
                access_token=access_token,
                account_id=account_id,
            ),
            False,
        )

    async def _record_qoder_state(
        self,
        account_id: str,
        result: CheckInResult,
        *,
        record_reauth: bool,
        state_error: str,
    ) -> None:
        if result.outcome in SUCCESS_OUTCOMES:
            await self._set_purpose(
                provider="qoder",
                account_id=account_id,
                status="active",
                verification_status="verified",
                success=True,
            )
        elif record_reauth and result.outcome in {
            CheckInOutcome.AUTH_FAILED,
            CheckInOutcome.NEEDS_REAUTH,
        }:
            await self._set_purpose(
                provider="qoder",
                account_id=account_id,
                status="needs_reauth",
                last_error=state_error,
            )

    async def _set_purpose(
        self,
        *,
        provider: str,
        account_id: str,
        status: str,
        verification_status: str | None = None,
        success: bool = False,
        last_error: str | None = None,
    ) -> None:
        purposes = await self._repo.list_purposes(provider, account_id)
        current = next(
            (item for item in purposes if item["purpose"] == "checkin"),
            None,
        )
        if current is None:
            return
        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        await self._repo.upsert_purpose(
            provider=provider,
            account_id=account_id,
            purpose="checkin",
            enabled=True if success else current["enabled"],
            status=status,
            verification_status=verification_status or current["verification_status"],
            capabilities=current.get("capabilities"),
            verified_at=now if verification_status == "verified" else current.get("verified_at"),
            expires_at=current.get("expires_at"),
            last_success_at=now if success else current.get("last_success_at"),
            failure_count=current.get("failure_count", 0),
            last_error=last_error,
        )
        await self._registry.rebuild()
