"""CodeBuddy OAuth device/plugin client.

AUTH-01: state/token/poll shapes from workbuddy_api reference; refresh unverified.
Never log or return raw tokens.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

# AUTH-01 anchors
CODEBUDDY_BASE = "https://copilot.tencent.com"
AUTH_STATE_URL = f"{CODEBUDDY_BASE}/v2/plugin/auth/state"
AUTH_TOKEN_URL = f"{CODEBUDDY_BASE}/v2/plugin/auth/token"

PENDING_CODE = 11217


class CodeBuddyOAuthError(Exception):
    """Upstream OAuth start/poll failure (redacted)."""


def _request_id() -> str:
    return uuid.uuid4().hex


def _auth_start_headers() -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Requested-With": "XMLHttpRequest",
        "X-Domain": "copilot.tencent.com",
        "X-No-Authorization": "true",
        "X-No-User-Id": "true",
        "X-No-Enterprise-Id": "true",
        "X-No-Department-Info": "true",
        "User-Agent": "CLI/1.0.8 CodeBuddy/1.0.8",
        "X-Product": "SaaS",
        "X-Request-ID": _request_id(),
    }


def _auth_poll_headers() -> dict[str, str]:
    rid = _request_id()
    span = secrets.token_hex(8)
    return {
        "Accept": "application/json, text/plain, */*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "X-Requested-With": "XMLHttpRequest",
        "X-Request-ID": rid,
        "b3": f"{rid}-{span}-1-",
        "X-B3-TraceId": rid,
        "X-B3-ParentSpanId": "",
        "X-B3-SpanId": span,
        "X-B3-Sampled": "1",
        "X-No-Authorization": "true",
        "X-No-User-Id": "true",
        "X-No-Enterprise-Id": "true",
        "X-No-Department-Info": "true",
        "X-Domain": "copilot.tencent.com",
        "User-Agent": "CLI/1.0.8 CodeBuddy/1.0.8",
        "X-Product": "SaaS",
    }


@dataclass(slots=True)
class OAuthStartResult:
    auth_state: str
    auth_url: str


@dataclass(slots=True)
class OAuthPollResult:
    """Poll outcome. Secrets only on fields; __repr__ redacts them."""

    status: Literal["pending", "success", "error"]
    code: int | None = None
    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None
    token_type: str | None = None
    domain: str | None = None
    session_state: str | None = None
    message: str | None = None
    # AUTH-01: refresh endpoint/contract unverified — not populated here
    _extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        return (
            f"OAuthPollResult(status={self.status!r}, code={self.code!r}, "
            f"access_token={'***' if self.access_token else None}, "
            f"refresh_token={'***' if self.refresh_token else None}, "
            f"expires_in={self.expires_in!r}, token_type={self.token_type!r}, "
            f"domain={self.domain!r}, message={self.message!r})"
        )


class CodeBuddyOAuthClient:
    """Minimal plugin OAuth client. Inject AsyncClient for tests."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        base_url: str = CODEBUDDY_BASE,
        timeout: float = 20.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    @property
    def state_url(self) -> str:
        return f"{self._base}/v2/plugin/auth/state"

    @property
    def token_url(self) -> str:
        return f"{self._base}/v2/plugin/auth/token"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def start(self) -> OAuthStartResult:
        """POST auth/state → auth_state + auth_url. AUTH-01."""
        nonce = secrets.token_hex(8)
        url = f"{self.state_url}?platform=CLI&nonce={nonce}"
        try:
            resp = await self._client.post(
                url, json={"nonce": nonce}, headers=_auth_start_headers()
            )
            data = resp.json()
        except Exception as exc:
            raise CodeBuddyOAuthError("auth_start_request_failed") from exc

        if resp.status_code != 200 or not isinstance(data, dict):
            raise CodeBuddyOAuthError("auth_start_http_error")

        if data.get("code") != 0 or not isinstance(data.get("data"), dict):
            raise CodeBuddyOAuthError("auth_start_failed")

        d = data["data"]
        auth_state = d.get("state")
        auth_url = d.get("authUrl") or d.get("auth_url")
        if not auth_state or not auth_url:
            raise CodeBuddyOAuthError("auth_start_missing_fields")
        return OAuthStartResult(auth_state=str(auth_state), auth_url=str(auth_url))

    async def poll(self, state: str) -> OAuthPollResult:
        """GET auth/token?state=… → pending | success | error. AUTH-01."""
        if not state:
            return OAuthPollResult(status="error", message="missing_state")
        data = await self._poll_data(state)
        return _poll_result(data)

    async def _poll_data(self, state: str) -> Any:
        url = f"{self.token_url}?state={state}"
        try:
            response = await self._client.get(url, headers=_auth_poll_headers())
            return response.json()
        except Exception:
            return _PollRequestFailure()


class _PollRequestFailure:
    pass


def _poll_result(data: Any) -> OAuthPollResult:
    if isinstance(data, _PollRequestFailure):
        return OAuthPollResult(status="error", message="auth_poll_request_failed")
    if not isinstance(data, dict):
        return OAuthPollResult(status="error", message="auth_poll_invalid_json")
    code = data.get("code")
    if code == PENDING_CODE:
        return OAuthPollResult(status="pending", code=PENDING_CODE, message="waiting")
    if code == 0 and isinstance(data.get("data"), dict):
        return _successful_poll_result(data["data"])
    return OAuthPollResult(
        status="error",
        code=int(code) if isinstance(code, int) else None,
        message="auth_poll_failed",
    )


def _successful_poll_result(data: dict[str, Any]) -> OAuthPollResult:
    access = data.get("accessToken") or data.get("access_token")
    if not access:
        return OAuthPollResult(status="error", code=0, message="auth_poll_no_token")
    refresh = data.get("refreshToken") or data.get("refresh_token")
    return OAuthPollResult(
        status="success",
        code=0,
        access_token=str(access),
        refresh_token=str(refresh) if refresh else None,
        expires_in=_expires_in(data),
        token_type=str(data.get("tokenType") or data.get("token_type") or "Bearer"),
        domain=str(data["domain"]) if data.get("domain") else None,
        session_state=str(data["sessionState"]) if data.get("sessionState") else None,
    )


def _expires_in(data: dict[str, Any]) -> int | None:
    value = data.get("expiresIn") if "expiresIn" in data else data.get("expires_in")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
