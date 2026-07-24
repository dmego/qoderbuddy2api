"""WorkBuddy / CodeBuddy check-in client (CB-CHECKIN-01).

Paths and auth mode are configurable; empty status_method skips preflight.
HTTP 400 + business code 10001 → ALREADY_CHECKED_IN (confirmed fact).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import httpx

from .base import (
    classify_http_error,
    classify_transport_error,
    extract_business_code,
    extract_message,
    extract_request_id,
    join_url,
    parse_json_body,
)
from .models import CheckInOutcome, CheckInResult

logger = logging.getLogger("qb2api.checkin.codebuddy")

AuthMode = Literal["bearer", "cookie", "bearer_cookie"]
_AUTH_MODES = frozenset({"bearer", "cookie", "bearer_cookie"})

_ALREADY_CODE = 10001


class WorkBuddyClient:
    """Single-account WorkBuddy daily check-in HTTP client.

    Each prepared request replaces/removes Cookie so the client's response jar
    cannot carry one account's session into another account request.
    """

    provider = "codebuddy"

    def __init__(
        self,
        *,
        base_url: str = "https://www.workbuddy.cn",
        status_path: str = "/billing/meter/checkin-status",
        status_method: str = "",
        claim_path: str = "/billing/meter/daily-checkin",
        claim_method: str = "POST",
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.status_path = status_path
        self.status_method = (status_method or "").strip().upper()
        self.claim_path = claim_path
        self.claim_method = (claim_method or "POST").strip().upper() or "POST"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            cookies=httpx.Cookies(),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def close(self) -> None:
        await self.aclose()

    def _build_headers(
        self,
        *,
        auth_mode: AuthMode | str,
        access_token: str | None,
        cookie: str | None,
        extra_headers: dict[str, str] | None,
    ) -> dict[str, str]:
        if auth_mode not in _AUTH_MODES:
            raise ValueError(f"unsupported auth_mode: {auth_mode}")
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if auth_mode in ("bearer", "bearer_cookie"):
            if not access_token:
                raise ValueError("access_token required for bearer auth")
            headers["Authorization"] = f"Bearer {access_token}"
        if auth_mode in ("cookie", "bearer_cookie"):
            if not cookie:
                raise ValueError("cookie required for cookie auth")
            headers["Cookie"] = cookie
        if extra_headers:
            allowed = {"x-user-id", "x-enterprise-id", "x-tenant-id", "x-domain"}
            for k, v in extra_headers.items():
                if k.lower() in allowed:
                    headers[k] = v
        return headers

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None = None,
    ) -> httpx.Response:
        """Prepare an exact per-account Cookie header and disable redirects."""
        request = self._client.build_request(
            method,
            url,
            headers=headers,
            content=content,
        )
        if "Cookie" in headers:
            request.headers["Cookie"] = headers["Cookie"]
        elif "Cookie" in request.headers:
            del request.headers["Cookie"]
        return await self._client.send(request, follow_redirects=False)

    async def checkin(
        self,
        *,
        account_id: str = "",
        auth_mode: AuthMode | str = "bearer",
        access_token: str | None = None,
        cookie: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> CheckInResult:
        """Run status preflight (if configured) then claim."""
        try:
            headers = self._build_headers(
                auth_mode=auth_mode,
                access_token=access_token,
                cookie=cookie,
                extra_headers=extra_headers,
            )
        except ValueError as e:
            return CheckInResult(
                outcome=CheckInOutcome.AUTH_FAILED,
                provider=self.provider,
                account_id=account_id,
                message=str(e),
            )

        if self.status_method:
            pre = await self._status(account_id=account_id, headers=headers)
            if pre is not None:
                return pre

        return await self._claim(account_id=account_id, headers=headers)

    async def _status(
        self,
        *,
        account_id: str,
        headers: dict[str, str],
    ) -> CheckInResult | None:
        """Return an auth/already result; ambiguous status proceeds to claim."""
        url = join_url(self.base_url, self.status_path)
        try:
            resp = await self._request(self.status_method, url, headers=headers)
        except httpx.HTTPError as exc:
            logger.debug("workbuddy status preflight transport error: %s", type(exc).__name__)
            return None

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

        if resp.status_code == 429:
            return classify_http_error(
                provider=self.provider,
                account_id=account_id,
                status_code=resp.status_code,
                body=body,
                request_id=rid,
            )

        if body and self._looks_already_checked(body):
            return CheckInResult(
                outcome=CheckInOutcome.ALREADY_CHECKED_IN,
                provider=self.provider,
                account_id=account_id,
                http_status=resp.status_code,
                business_code=extract_business_code(body),
                request_id=rid,
                message=extract_message(body) or "already checked in (status)",
                raw_status=str(body.get("status") or body.get("state") or "") or None,
            )
        return None

    @staticmethod
    def _looks_already_checked(body: dict[str, Any]) -> bool:
        code = extract_business_code(body)
        if code is not None and str(code) == str(_ALREADY_CODE):
            return True
        status = str(body.get("status") or body.get("state") or "").upper()
        if status in {
            "CHECKED_IN",
            "ALREADY_CHECKED_IN",
            "CLAIMED",
            "CLAIMED_TODAY",
            "DONE",
        }:
            return True
        if body.get("checkedIn") is True or body.get("checked_in") is True:
            return True
        if body.get("canCheckIn") is False or body.get("can_check_in") is False:
            # only if explicit already flag also present — avoid false positive
            if body.get("alreadyCheckedIn") or body.get("already_checked_in"):
                return True
        return bool(body.get("alreadyCheckedIn") or body.get("already_checked_in"))

    async def _claim(
        self,
        *,
        account_id: str,
        headers: dict[str, str],
    ) -> CheckInResult:
        url = join_url(self.base_url, self.claim_path)
        try:
            resp = await self._request(
                self.claim_method,
                url,
                headers=headers,
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
        code = extract_business_code(body)
        msg = extract_message(body)
        return self._classify_claim(
            account_id=account_id,
            response=resp,
            body=body,
            request_id=rid,
            business_code=code,
            message=msg,
        )

    def _classify_claim(
        self,
        *,
        account_id: str,
        response: httpx.Response,
        body: dict[str, Any] | None,
        request_id: str | None,
        business_code: str | int | None,
        message: str | None,
    ) -> CheckInResult:
        already = business_code is not None and str(business_code) == str(_ALREADY_CODE)
        if already and (response.status_code == 400 or response.is_success):
            return CheckInResult(
                outcome=CheckInOutcome.ALREADY_CHECKED_IN,
                provider=self.provider,
                account_id=account_id,
                http_status=response.status_code,
                business_code=business_code,
                request_id=request_id,
                message=message or "already checked in today",
            )
        if response.is_success:
            return CheckInResult(
                outcome=CheckInOutcome.CLAIMED,
                provider=self.provider,
                account_id=account_id,
                http_status=response.status_code,
                business_code=business_code,
                request_id=request_id,
                message=message,
            )
        return classify_http_error(
            provider=self.provider,
            account_id=account_id,
            status_code=response.status_code,
            body=body,
            request_id=request_id,
        )


# Alias used in design text.
CodeBuddyCheckinClient = WorkBuddyClient
