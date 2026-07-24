"""Qoder check-in client (QD-CHECKIN-01 reference).

GET status / POST claim {} / POST refresh.
Maps CLAIMED_TODAY / CLAIMED / ALREADY_CLAIMED.
Refresh accepts device_token or token as new access.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import (
    classify_http_error,
    classify_transport_error,
    extract_message,
    extract_request_id,
    join_url,
    parse_json_body,
)
from .models import CheckInOutcome, CheckInResult, RefreshResult

logger = logging.getLogger("qb2api.checkin.qoder")

_ALREADY_STATUS = frozenset(
    {
        "CLAIMED_TODAY",
        "ALREADY_CLAIMED",
        "ALREADY_CHECKED_IN",
        "CHECKED_IN",
    }
)
_CLAIMABLE_STATUS = frozenset({"CLAIMABLE", "NOT_CLAIMED", "AVAILABLE"})
_CLAIMED_RESULT = frozenset({"CLAIMED", "SUCCESS", "OK"})
_ALREADY_RESULT = frozenset({"ALREADY_CLAIMED", "CLAIMED_TODAY", "ALREADY_CHECKED_IN"})


class QoderCheckinClient:
    """Qoder Sash daily check-in + deviceToken refresh client."""

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
        """GET daily check-in status without claiming.

        Returns:
          ALREADY_CHECKED_IN if status is CLAIMED_TODAY / ALREADY_*
          CLAIMED is NOT used here — success of status alone is reported as SKIPPED? Better:
          Use outcome ALREADY_CHECKED_IN or a neutral path.

        For orchestration, status() returns ALREADY_CHECKED_IN or a non-terminal
        FAILED/NEEDS_REAUTH/…; when claimable, outcome is SKIPPED with raw_status=CLAIMABLE
        so callers know to claim (ponytail: SKIPPED = "proceed to claim" marker).
        """
        url = join_url(self.base_url, self.status_path)
        try:
            resp = await self._client.get(url, headers=self._auth_headers(access_token))
        except httpx.HTTPError as exc:
            return classify_transport_error(
                provider=self.provider,
                account_id=account_id,
                exc=exc,
            )

        body = parse_json_body(resp.text)
        rid = extract_request_id(body, resp.headers)

        if resp.status_code in (401, 403):
            return classify_http_error(
                provider=self.provider,
                account_id=account_id,
                status_code=resp.status_code,
                body=body,
                request_id=rid,
            )
        if not (200 <= resp.status_code < 300):
            return classify_http_error(
                provider=self.provider,
                account_id=account_id,
                status_code=resp.status_code,
                body=body,
                request_id=rid,
            )

        status_val = self._extract_status(body)
        reward = self._extract_reward(body)

        if status_val and status_val.upper() in _ALREADY_STATUS:
            return CheckInResult(
                outcome=CheckInOutcome.ALREADY_CHECKED_IN,
                provider=self.provider,
                account_id=account_id,
                http_status=resp.status_code,
                request_id=rid,
                message=extract_message(body),
                reward_credits=reward,
                raw_status=status_val,
            )

        if status_val and status_val.upper() in _CLAIMABLE_STATUS:
            return CheckInResult(
                outcome=CheckInOutcome.SKIPPED,
                provider=self.provider,
                account_id=account_id,
                http_status=resp.status_code,
                request_id=rid,
                message=extract_message(body),
                reward_credits=reward,
                raw_status=status_val,
            )
        return CheckInResult(
            outcome=CheckInOutcome.FAILED,
            provider=self.provider,
            account_id=account_id,
            http_status=resp.status_code,
            request_id=rid,
            message=extract_message(body) or "unrecognized check-in status",
            reward_credits=reward,
            raw_status=status_val,
        )

    async def claim(
        self,
        *,
        access_token: str,
        account_id: str = "",
    ) -> CheckInResult:
        """POST claim with empty JSON body."""
        url = join_url(self.base_url, self.claim_path)
        try:
            resp = await self._client.post(
                url,
                headers=self._auth_headers(access_token),
                content=b"{}",
            )
        except httpx.HTTPError as exc:
            return classify_transport_error(
                provider=self.provider,
                account_id=account_id,
                exc=exc,
            )

        body = parse_json_body(resp.text)
        rid = extract_request_id(body, resp.headers)
        result_val = self._extract_claim_result(body)
        reward = self._extract_reward(body)

        if resp.status_code in (401, 403):
            return classify_http_error(
                provider=self.provider,
                account_id=account_id,
                status_code=resp.status_code,
                body=body,
                request_id=rid,
            )

        if 200 <= resp.status_code < 300:
            if result_val and result_val.upper() in _ALREADY_RESULT:
                return CheckInResult(
                    outcome=CheckInOutcome.ALREADY_CHECKED_IN,
                    provider=self.provider,
                    account_id=account_id,
                    http_status=resp.status_code,
                    request_id=rid,
                    message=extract_message(body),
                    reward_credits=reward,
                    raw_status=result_val,
                )
            if result_val and result_val.upper() in _CLAIMED_RESULT:
                return CheckInResult(
                    outcome=CheckInOutcome.CLAIMED,
                    provider=self.provider,
                    account_id=account_id,
                    http_status=resp.status_code,
                    request_id=rid,
                    message=extract_message(body),
                    reward_credits=reward,
                    raw_status=result_val,
                )
            return CheckInResult(
                outcome=CheckInOutcome.FAILED,
                provider=self.provider,
                account_id=account_id,
                http_status=resp.status_code,
                request_id=rid,
                message=extract_message(body) or "unrecognized check-in result",
                reward_credits=reward,
                raw_status=result_val,
            )

        # non-2xx may still encode already-claimed
        if result_val and result_val.upper() in _ALREADY_RESULT:
            return CheckInResult(
                outcome=CheckInOutcome.ALREADY_CHECKED_IN,
                provider=self.provider,
                account_id=account_id,
                http_status=resp.status_code,
                request_id=rid,
                message=extract_message(body),
                reward_credits=reward,
                raw_status=result_val,
            )

        return classify_http_error(
            provider=self.provider,
            account_id=account_id,
            status_code=resp.status_code,
            body=body,
            request_id=rid,
        )

    async def checkin(
        self,
        *,
        access_token: str,
        account_id: str = "",
    ) -> CheckInResult:
        """status → short-circuit ALREADY; else claim."""
        st = await self.status(access_token=access_token, account_id=account_id)
        if st.outcome == CheckInOutcome.ALREADY_CHECKED_IN:
            return st
        if st.outcome in (
            CheckInOutcome.NEEDS_REAUTH,
            CheckInOutcome.AUTH_FAILED,
            CheckInOutcome.RATE_LIMITED,
            CheckInOutcome.TRANSIENT_ERROR,
            CheckInOutcome.FAILED,
        ):
            return st
        return await self.claim(access_token=access_token, account_id=account_id)

    async def refresh(
        self,
        *,
        refresh_token: str,
        account_id: str = "",
    ) -> RefreshResult:
        """POST deviceToken/refresh. Accepts device_token or token as access.

        Does not log or return secrets in message fields beyond the token values
        that the caller must persist — callers must not log RefreshResult tokens.
        """
        if not refresh_token:
            return RefreshResult(
                outcome=CheckInOutcome.AUTH_FAILED,
                message="refresh_token required",
            )

        url = join_url(self.base_url, self.refresh_path)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        try:
            resp = await self._client.post(
                url,
                headers=headers,
                json={"refresh_token": refresh_token},
            )
        except httpx.HTTPError as exc:
            return RefreshResult(
                outcome=CheckInOutcome.TRANSIENT_ERROR,
                message=f"transport error: {type(exc).__name__}",
            )

        body = parse_json_body(resp.text)
        if not (200 <= resp.status_code < 300):
            outcome = CheckInOutcome.NEEDS_REAUTH if resp.status_code in (401, 403) else (
                CheckInOutcome.RATE_LIMITED if resp.status_code == 429 else (
                    CheckInOutcome.TRANSIENT_ERROR if resp.status_code >= 500 else CheckInOutcome.FAILED
                )
            )
            return RefreshResult(
                http_status=resp.status_code,
                outcome=outcome,
                message=extract_message(body) or f"http {resp.status_code}",
            )

        access = None
        new_refresh = None
        if body:
            access = body.get("device_token") or body.get("token") or body.get("access_token")
            if isinstance(access, str):
                access = access.strip() or None
            else:
                access = None
            raw_rt = body.get("refresh_token")
            if isinstance(raw_rt, str) and raw_rt.strip():
                new_refresh = raw_rt.strip()

        if not access:
            return RefreshResult(
                http_status=resp.status_code,
                outcome=CheckInOutcome.FAILED,
                message="refresh response missing access token",
            )

        return RefreshResult(
            access_token=access,
            refresh_token=new_refresh,
            http_status=resp.status_code,
            outcome=None,
            message=None,
        )

    @staticmethod
    def _extract_status(body: dict[str, Any] | None) -> str | None:
        if not body:
            return None
        for key in ("status", "checkInStatus", "checkin_status", "state"):
            val = body.get(key)
            if isinstance(val, str) and val:
                return val
        # nested data
        data = body.get("data")
        if isinstance(data, dict):
            for key in ("status", "checkInStatus", "state"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val
        return None

    @staticmethod
    def _extract_claim_result(body: dict[str, Any] | None) -> str | None:
        if not body:
            return None
        for key in ("result", "status", "claimResult"):
            val = body.get(key)
            if isinstance(val, str) and val:
                return val
        data = body.get("data")
        if isinstance(data, dict):
            for key in ("result", "status"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val
        return None

    @staticmethod
    def _extract_reward(body: dict[str, Any] | None) -> float | None:
        if not body:
            return None
        for key in ("rewardCredits", "reward_credits"):
            val = body.get(key)
            if isinstance(val, (int, float)):
                return float(val)
        data = body.get("data")
        if isinstance(data, dict):
            for key in ("rewardCredits", "reward_credits"):
                val = data.get(key)
                if isinstance(val, (int, float)):
                    return float(val)
        return None
