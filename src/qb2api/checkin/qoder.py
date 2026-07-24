"""Qoder Sash daily check-in and device-token refresh HTTP client."""

from __future__ import annotations

import httpx

from .base import classify_transport_error, join_url, parse_json_body
from .models import CheckInOutcome, CheckInResult, RefreshResult
from .qoder_status import (
    classify_claim,
    classify_refresh,
    classify_status,
    is_claimable,
)


class QoderCheckinClient:
    provider = "qoder"

    def __init__(
        self,
        *,
        base_url: str = "https://openapi.qoder.com.cn",
        status_path: str = "/sash/api/v1/me/daily-check-in/status",
        claim_path: str = "/sash/api/v1/me/daily-check-in/claim",
        refresh_path: str = "/api/v1/deviceToken/refresh",
        user_agent: str = "QoderWork",
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.status_path = status_path
        self.claim_path = claim_path
        self.refresh_path = refresh_path
        self.user_agent = user_agent
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def close(self) -> None:
        await self.aclose()

    def _auth_headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }

    async def status(
        self,
        *,
        access_token: str,
        account_id: str = "",
    ) -> CheckInResult:
        try:
            response = await self._client.get(
                join_url(self.base_url, self.status_path),
                headers=self._auth_headers(access_token),
            )
        except httpx.HTTPError as error:
            return classify_transport_error(
                provider=self.provider,
                account_id=account_id,
                exc=error,
            )
        return classify_status(
            status_code=response.status_code,
            body=parse_json_body(response.text),
            headers=response.headers,
            account_id=account_id,
        )

    async def claim(
        self,
        *,
        access_token: str,
        account_id: str = "",
    ) -> CheckInResult:
        try:
            response = await self._client.post(
                join_url(self.base_url, self.claim_path),
                headers=self._auth_headers(access_token),
                content=b"{}",
            )
        except httpx.HTTPError as error:
            return classify_transport_error(
                provider=self.provider,
                account_id=account_id,
                exc=error,
            )
        return classify_claim(
            status_code=response.status_code,
            body=parse_json_body(response.text),
            headers=response.headers,
            account_id=account_id,
        )

    async def checkin(
        self,
        *,
        access_token: str,
        account_id: str = "",
    ) -> CheckInResult:
        status = await self.status(
            access_token=access_token,
            account_id=account_id,
        )
        if not is_claimable(status):
            return status
        return await self.claim(
            access_token=access_token,
            account_id=account_id,
        )

    async def refresh(
        self,
        *,
        refresh_token: str,
        account_id: str = "",
    ) -> RefreshResult:
        del account_id
        if not refresh_token:
            return RefreshResult(
                outcome=CheckInOutcome.AUTH_FAILED,
                message="refresh_token required",
            )
        try:
            response = await self._client.post(
                join_url(self.base_url, self.refresh_path),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": self.user_agent,
                },
                json={"refresh_token": refresh_token},
            )
        except httpx.HTTPError as error:
            return RefreshResult(
                outcome=CheckInOutcome.TRANSIENT_ERROR,
                message=f"transport error: {type(error).__name__}",
            )
        return classify_refresh(
            status_code=response.status_code,
            body=parse_json_body(response.text),
        )
