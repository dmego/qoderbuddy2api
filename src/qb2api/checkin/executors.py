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
        self._workbuddy = workbuddy or _workbuddy_client(settings)
        self._qoder = qoder or _qoder_client(settings)

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
            return _missing_credential("codebuddy", account_id)
        mode = credential.mode
        result = await self._workbuddy.checkin(
            account_id=account_id,
            auth_mode=mode,
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
            return _missing_credential("qoder", account_id)
        access_token = credential.payload.get("access_token") or credential.payload.get("token")
        if not access_token:
            return _needs_reauth("qoder", account_id, "missing access_token")
        result = await self._qoder.checkin(
            access_token=access_token,
            account_id=account_id,
        )
        refresh_token = credential.payload.get("refresh_token")
        if result.outcome == CheckInOutcome.NEEDS_REAUTH and refresh_token:
            result = await self._refresh_qoder(
                account_id,
                credential=credential,
                refresh_token=refresh_token,
            )
        await self._record_qoder_state(account_id, result)
        return result

    async def _refresh_qoder(
        self,
        account_id: str,
        *,
        credential: Any,
        refresh_token: str,
    ) -> CheckInResult:
        refreshed = await self._qoder.refresh(
            refresh_token=refresh_token,
            account_id=account_id,
        )
        if not refreshed.ok or not refreshed.access_token:
            return await self._qoder_refresh_failure(account_id, refreshed)
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
            return _needs_reauth("qoder", account_id, "missing refreshed access_token")
        return await self._qoder.checkin(
            access_token=access_token,
            account_id=account_id,
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
        refreshed: RefreshResult,
    ) -> CheckInResult:
        outcome = refreshed.outcome or CheckInOutcome.FAILED
        if outcome in {CheckInOutcome.AUTH_FAILED, CheckInOutcome.NEEDS_REAUTH}:
            await self._set_purpose(
                provider="qoder",
                account_id=account_id,
                status="needs_reauth",
                last_error="refresh_failed",
            )
        return CheckInResult(
            outcome=outcome,
            provider="qoder",
            account_id=account_id,
            http_status=refreshed.http_status,
            message=refreshed.message or "refresh failed",
        )

    async def _record_qoder_state(
        self,
        account_id: str,
        result: CheckInResult,
    ) -> None:
        if result.outcome in SUCCESS_OUTCOMES:
            await self._set_purpose(
                provider="qoder",
                account_id=account_id,
                status="active",
                verification_status="verified",
                success=True,
            )
        elif result.outcome == CheckInOutcome.NEEDS_REAUTH:
            await self._set_purpose(
                provider="qoder",
                account_id=account_id,
                status="needs_reauth",
                last_error="auth_failed",
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


def _workbuddy_client(settings: Settings) -> WorkBuddyClient:
    return WorkBuddyClient(
        base_url=settings.codebuddy_checkin_base,
        status_path=settings.codebuddy_checkin_status_path,
        status_method=settings.codebuddy_checkin_status_method,
        claim_path=settings.codebuddy_checkin_claim_path,
        claim_method=settings.codebuddy_checkin_claim_method,
        timeout=float(settings.checkin_request_timeout_seconds),
    )


def _qoder_client(settings: Settings) -> QoderCheckinClient:
    return QoderCheckinClient(
        base_url=settings.qoder_checkin_base,
        status_path=settings.qoder_checkin_status_path,
        claim_path=settings.qoder_checkin_claim_path,
        refresh_path=settings.qoder_checkin_refresh_path,
        timeout=float(settings.checkin_request_timeout_seconds),
    )


def _missing_credential(provider: str, account_id: str) -> CheckInResult:
    return CheckInResult(
        outcome=CheckInOutcome.SKIPPED,
        provider=provider,
        account_id=account_id,
        message="no checkin credential",
    )


def _needs_reauth(provider: str, account_id: str, message: str) -> CheckInResult:
    return CheckInResult(
        outcome=CheckInOutcome.NEEDS_REAUTH,
        provider=provider,
        account_id=account_id,
        message=message,
    )
