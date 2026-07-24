"""Account domain types — enums and lightweight records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ProviderName = Literal["codebuddy", "qoder"]
PurposeName = Literal["chat", "checkin"]
AccountSource = Literal["oauth", "manual", "env", "import"]

PurposeStatus = Literal[
    "unconfigured",
    "needs_import",
    "active",
    "expired",
    "needs_reauth",
    "disabled",
    "invalid",
]

VerificationStatus = Literal[
    "not_required",
    "unverified",
    "verified",
    "rejected",
]

CredentialMode = Literal[
    "bearer",
    "cookie",
    "bearer_cookie",
    "oauth",
    "pat",
    "access_refresh",
]


@dataclass(slots=True)
class AccountRecord:
    provider: str
    account_id: str
    label: str
    source: str
    enabled: bool
    masked_identity: str | None = None
    identity_hash: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class PurposeRecord:
    provider: str
    account_id: str
    purpose: str
    enabled: bool
    status: PurposeStatus
    verification_status: VerificationStatus
    capabilities: list[str] = field(default_factory=list)
    verified_at: str | None = None
    expires_at: str | None = None
    last_success_at: str | None = None
    failure_count: int = 0
    last_error: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class CredentialRecord:
    provider: str
    account_id: str
    purpose: str
    mode: str
    encrypted_payload: str
    has_refresh_token: bool = False
    payload_version: int = 1
    credential_version: int = 1
    fingerprint_hmac: str | None = None
    expires_at: str | None = None
    updated_at: str | None = None
    id: int | None = None


@dataclass(slots=True)
class AccountView:
    """UI-safe account view — never includes secrets."""

    provider: str
    account_id: str
    label: str
    source: str
    enabled: bool
    summary_status: str
    purposes: dict[str, dict[str, Any]] = field(default_factory=dict)
    masked_identity: str | None = None
    shadowed: bool = False
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(slots=True)
class AccountSlot:
    """Pool membership slot for a single purpose — no secrets."""

    provider: str
    account_id: str
    source: str
    enabled: bool
    status: str
    verification_status: str
    shadowed: bool = False
    capabilities: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Credential:
    """In-process credential snapshot. Never log or return to UI."""

    provider: str
    account_id: str
    purpose: str
    mode: str
    payload: dict[str, Any]
    credential_version: int
    expires_at: str | None = None
    has_refresh_token: bool = False
