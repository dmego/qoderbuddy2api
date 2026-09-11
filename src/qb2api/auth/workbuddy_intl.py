"""WorkBuddy international (www.workbuddy.ai) auth and credential client.

The international deployment shares the domestic plugin OAuth shape but lives
on its own origin and Keycloak realm:

- ``POST /v2/plugin/auth/state`` mints ``{state, authUrl}``
- the user signs in at ``/login?platform=CLI&state=…``
- ``GET /v2/plugin/auth/token?state=…`` returns the bearer pair once done
- ``POST /v2/plugin/auth/token/refresh`` with ``X-Refresh-Token`` rotates it

Never log or return raw tokens.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

WORKBUDDY_INTL_BASE = "https://www.workbuddy.ai"
PENDING_CODE = 11217


class WorkBuddyIntlAuthError(Exception):
    """Upstream auth start/poll/refresh failure (redacted)."""


def _request_id() -> str:
    return uuid.uuid4().hex


def _auth_headers() -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Requested-With": "XMLHttpRequest",
        "X-Domain": "www.workbuddy.ai",
        "X-No-Authorization": "true",
        "X-No-User-Id": "true",
        "X-No-Enterprise-Id": "true",
        "X-No-Department-Info": "true",
        "User-Agent": "CLI/1.0.8 CodeBuddy/1.0.8",
        "X-Product": "SaaS",
        "X-Request-ID": _request_id(),
    }


def _refresh_headers(refresh_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Domain": "www.workbuddy.ai",
        "X-Refresh-Token": refresh_token,
        "X-Auth-Refresh-Source": "plugin",
        "User-Agent": "CLI/1.0.8 CodeBuddy/1.0.8",
        "X-Product": "SaaS",
        "X-Request-ID": _request_id(),
    }


@dataclass(slots=True)
class IntlAuthStart:
    auth_state: str
    auth_url: str


@dataclass(slots=True)
class IntlAuthResult:
    """Poll/refresh outcome. Secrets stay on fields; __repr__ redacts them."""

    status: Literal["pending", "success", "error"]
    code: int | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None
    domain: str | None = None
    message: str | None = None
    _extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        return (
            f"IntlAuthResult(status={self.status!r}, code={self.code!r}, "
            f"access_token={'***' if self.access_token else None}, "
            f"refresh_token={'***' if self.refresh_token else None}, "
            f"expires_in={self.expires_in!r}, domain={self.domain!r}, "
            f"message={self.message!r})"
        )


class WorkBuddyIntlAuthClient:
    """Plugin OAuth client for the international deployment."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        base_url: str = WORKBUDDY_INTL_BASE,
        timeout: float = 20.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            trust_env=False,
        )

    @property
    def state_url(self) -> str:
        return f"{self._base}/v2/plugin/auth/state"

    @property
    def token_url(self) -> str:
        return f"{self._base}/v2/plugin/auth/token"

    @property
    def refresh_url(self) -> str:
        return f"{self._base}/v2/plugin/auth/token/refresh"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def start(self) -> IntlAuthStart:
        nonce = secrets.token_hex(8)
        url = f"{self.state_url}?platform=CLI&nonce={nonce}"
        try:
            response = await self._client.post(
                url, json={"nonce": nonce}, headers=_auth_headers()
            )
            payload = response.json()
        except Exception as error:
            raise WorkBuddyIntlAuthError("auth_start_request_failed") from error
        if response.status_code != 200 or not isinstance(payload, dict):
            raise WorkBuddyIntlAuthError("auth_start_http_error")
        if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
            raise WorkBuddyIntlAuthError("auth_start_failed")
        data = payload["data"]
        state = data.get("state")
        auth_url = data.get("authUrl") or data.get("auth_url")
        if not state or not auth_url:
            raise WorkBuddyIntlAuthError("auth_start_missing_fields")
        return IntlAuthStart(auth_state=str(state), auth_url=str(auth_url))

    async def poll(self, state: str) -> IntlAuthResult:
        if not state:
            return IntlAuthResult(status="error", message="missing_state")
        try:
            response = await self._client.get(
                f"{self.token_url}?state={state}", headers=_auth_headers()
            )
            payload = response.json()
        except Exception:
            return IntlAuthResult(status="error", message="auth_poll_request_failed")
        return _poll_result(payload)

    async def refresh(self, refresh_token: str) -> IntlAuthResult:
        """Rotate a bearer pair. Upstream keeps the same refresh token value."""
        if not refresh_token:
            return IntlAuthResult(status="error", message="missing_refresh_token")
        try:
            response = await self._client.post(
                self.refresh_url, json={}, headers=_refresh_headers(refresh_token)
            )
            payload = response.json()
        except Exception:
            return IntlAuthResult(status="error", message="auth_refresh_request_failed")
        return _poll_result(payload)


def _poll_result(payload: Any) -> IntlAuthResult:
    if not isinstance(payload, dict):
        return IntlAuthResult(status="error", message="auth_invalid_json")
    code = payload.get("code")
    if code == PENDING_CODE:
        return IntlAuthResult(status="pending", code=PENDING_CODE, message="waiting")
    if code == 0 and isinstance(payload.get("data"), dict):
        return _success_result(payload["data"])
    return IntlAuthResult(
        status="error",
        code=int(code) if isinstance(code, int) else None,
        message=str(payload.get("msg") or "auth_failed")[:200],
    )


def _success_result(data: dict[str, Any]) -> IntlAuthResult:
    access = data.get("accessToken") or data.get("access_token")
    if not access:
        return IntlAuthResult(status="error", code=0, message="auth_no_token")
    refresh = data.get("refreshToken") or data.get("refresh_token")
    return IntlAuthResult(
        status="success",
        code=0,
        access_token=str(access),
        refresh_token=str(refresh) if refresh else None,
        expires_in=_expires_in(data),
        domain=str(data["domain"]) if data.get("domain") else None,
    )


def _expires_in(data: dict[str, Any]) -> int | None:
    value = data.get("expiresIn") if "expiresIn" in data else data.get("expires_in")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
