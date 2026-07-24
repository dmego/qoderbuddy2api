"""Check-in domain models (design §9.4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CheckInOutcome(StrEnum):
    """Normalized check-in result for persistence and scheduling."""

    CLAIMED = "CLAIMED"
    ALREADY_CHECKED_IN = "ALREADY_CHECKED_IN"
    AUTH_FAILED = "AUTH_FAILED"
    NEEDS_REAUTH = "NEEDS_REAUTH"
    RATE_LIMITED = "RATE_LIMITED"
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


# Terminal business-success outcomes (no retry, no proxy impact).
SUCCESS_OUTCOMES = frozenset(
    {
        CheckInOutcome.CLAIMED,
        CheckInOutcome.ALREADY_CHECKED_IN,
    }
)


@dataclass(slots=True)
class CheckInResult:
    """Single-account check-in attempt result (redacted)."""

    outcome: CheckInOutcome
    provider: str
    account_id: str = ""
    http_status: int | None = None
    business_code: str | int | None = None
    request_id: str | None = None
    message: str | None = None
    reward_credits: float | None = None
    raw_status: str | None = None  # upstream status string, e.g. CLAIMED_TODAY
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome in SUCCESS_OUTCOMES


@dataclass(slots=True)
class RefreshResult:
    """Qoder device token refresh result (secrets only in caller scope)."""

    access_token: str | None = None
    refresh_token: str | None = None  # only set when upstream rotated it
    http_status: int | None = None
    outcome: CheckInOutcome | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.access_token)
