"""Small construction and result helpers for the check-in executor."""

from __future__ import annotations

from qb2api.config import Settings

from .codebuddy import WorkBuddyClient
from .models import CheckInOutcome, CheckInResult
from .qoder import QoderCheckinClient


def workbuddy_client(settings: Settings) -> WorkBuddyClient:
    return WorkBuddyClient(
        base_url=settings.codebuddy_checkin_base,
        status_path=settings.codebuddy_checkin_status_path,
        status_method=settings.codebuddy_checkin_status_method,
        claim_path=settings.codebuddy_checkin_claim_path,
        claim_method=settings.codebuddy_checkin_claim_method,
        timeout=float(settings.checkin_request_timeout_seconds),
    )


def qoder_client(settings: Settings) -> QoderCheckinClient:
    return QoderCheckinClient(
        base_url=settings.qoder_checkin_base,
        status_path=settings.qoder_checkin_status_path,
        claim_path=settings.qoder_checkin_claim_path,
        refresh_path=settings.qoder_checkin_refresh_path,
        timeout=float(settings.checkin_request_timeout_seconds),
    )


def missing_credential(provider: str, account_id: str) -> CheckInResult:
    return CheckInResult(
        outcome=CheckInOutcome.SKIPPED,
        provider=provider,
        account_id=account_id,
        message="no checkin credential",
    )


def needs_reauth(provider: str, account_id: str, message: str) -> CheckInResult:
    return CheckInResult(
        outcome=CheckInOutcome.NEEDS_REAUTH,
        provider=provider,
        account_id=account_id,
        message=message,
    )
