"""OrcaTerm (Tencent Cloud lightai) browser-login auth client.

Login is a two-step handshake against the OrcaTerm console:

1. The operator opens ``https://orcaterm.com/oauth/authorize?provider=txcloud&
   source=web&return_url=<this admin page>&session_id=<uuid>``. The console runs
   the Tencent Cloud OAuth dance and finally lands the browser on that
   ``return_url`` with ``?session_id=<same uuid>`` appended.
2. The Control Plane polls ``OAuthExchangeToken`` (no bearer needed) with that
   session id until the console reports success, then receives
   ``{"AccessToken": ..., "ExpiresIn": ...}``.

The access token is the same JWT the desktop app stores in ``data.bin``, so a
browser login and a manual paste produce interchangeable credentials.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

ORCATERM_AUTHORIZE_URL = "https://orcaterm.com/oauth/authorize"
ORCATERM_DEFAULT_PROVIDER = "txcloud"


class OrcaTermAuthError(Exception):
    """Upstream auth start/poll failure (redacted)."""


@dataclass(slots=True)
class OrcaTermAuthStart:
    session_id: str
    auth_url: str


@dataclass(slots=True)
class OrcaTermAuthResult:
    """Poll outcome. Secrets stay on fields; __repr__ redacts them."""

    status: str
    access_token: str | None = field(default=None, repr=False)
    expires_in: int | None = None
    message: str | None = None


def build_authorize_url(
    *,
    return_url: str,
    session_id: str | None = None,
    provider: str = ORCATERM_DEFAULT_PROVIDER,
) -> OrcaTermAuthStart:
    """Build the console authorize URL carrying our callback and session id."""
    session = session_id or str(uuid.uuid4())
    query = urlencode(
        {
            "provider": provider,
            "source": "web",
            "return_url": return_url,
            "session_id": session,
        }
    )
    return OrcaTermAuthStart(session_id=session, auth_url=f"{ORCATERM_AUTHORIZE_URL}?{query}")


class OrcaTermAuthClient:
    """Console OAuth exchange client (no bearer required)."""

    def __init__(
        self,
        *,
        endpoint: str = "https://api.orcaterm.cloud.tencent.com",
        timeout: int = 20,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10),
            trust_env=False,
        )

    async def exchange(self, session_id: str) -> OrcaTermAuthResult:
        """Exchange a completed login session for an access token."""
        payload = await self._cgi("OAuthExchangeToken", {"sessionId": session_id})
        response = payload.get("Response")
        if isinstance(response, dict) and response.get("Error"):
            error = response["Error"]
            return OrcaTermAuthResult(
                status="error",
                message=str(error.get("Message") or error.get("Code") or "auth_failed"),
            )
        data = response if isinstance(response, dict) else payload
        token = data.get("AccessToken") or data.get("accessToken")
        if not token:
            return OrcaTermAuthResult(status="error", message="auth_failed")
        return OrcaTermAuthResult(
            status="success",
            access_token=str(token),
            expires_in=_positive_int(data.get("ExpiresIn") or data.get("expiresIn")),
        )

    async def _cgi(self, action: str, data: dict) -> dict:
        url = f"{self.endpoint}/cgi/api?{urlencode({'action': action})}"
        headers = {
            "Content-Type": "application/json",
            "X-Product": "orcaterm",
            "X-SeqId": uuid.uuid4().hex,
            "Origin": "https://orcaterm.cloud.tencent.com",
            "Referer": "https://orcaterm.cloud.tencent.com/",
            "User-Agent": "OrcaTerm/1.5.2",
        }
        try:
            response = await self._client.post(
                url, json={"action": action, "data": data}, headers=headers
            )
        except httpx.HTTPError as error:
            raise OrcaTermAuthError(f"orcaterm auth request failed: {error}") from error
        try:
            decoded = response.json()
        except ValueError as error:
            raise OrcaTermAuthError("orcaterm auth returned a non-JSON body") from error
        if not isinstance(decoded, dict):
            raise OrcaTermAuthError("orcaterm auth returned an unexpected payload")
        inner = decoded.get("data")
        if isinstance(inner, dict):
            return inner
        return decoded

    async def close(self) -> None:
        await self._client.aclose()


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    return number if number > 0 else None


__all__ = [
    "ORCATERM_AUTHORIZE_URL",
    "ORCATERM_DEFAULT_PROVIDER",
    "OrcaTermAuthClient",
    "OrcaTermAuthError",
    "OrcaTermAuthResult",
    "OrcaTermAuthStart",
    "build_authorize_url",
]
