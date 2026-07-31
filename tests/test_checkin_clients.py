"""WorkBuddy check-in client contracts (CB-CHECKIN-01)."""

from __future__ import annotations

import json

import httpx
import pytest

from qb2api.checkin import CheckInOutcome, WorkBuddyClient


def _json_response(status: int, body: dict | str | None, request: httpx.Request) -> httpx.Response:
    if body is None:
        content, headers = b"", {}
    elif isinstance(body, str):
        content, headers = body.encode(), {"content-type": "application/json"}
    else:
        content, headers = json.dumps(body).encode(), {"content-type": "application/json"}
    return httpx.Response(status, content=content, headers=headers, request=request)


@pytest.mark.asyncio
async def test_workbuddy_claim_success_without_status_preflight():
    """Empty status_method skips status; POST claim 2xx claims the reward."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        assert request.headers.get("Authorization") == "Bearer tok-a"
        assert request.content == b"{}"
        return _json_response(200, {"code": 0, "msg": "ok", "requestId": "r1"}, request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://www.workbuddy.cn"
    ) as http:
        result = await WorkBuddyClient(
            base_url="https://www.workbuddy.cn", status_method="", client=http
        ).checkin(account_id="cb-1", auth_mode="bearer", access_token="tok-a")

    assert result.outcome == CheckInOutcome.CLAIMED
    assert result.request_id == "r1"
    assert result.ok
    assert calls == [("POST", "/billing/meter/daily-checkin")]


@pytest.mark.asyncio
async def test_workbuddy_http_400_code_10001_is_already_checked_in():
    """HTTP 400 plus business code 10001 remains a successful terminal result."""
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            400,
            {
                "code": 10001,
                "msg": "今天已签到，请明天再来",
                "requestId": "rid-already",
            },
            request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await WorkBuddyClient(status_method="", client=http).checkin(
            auth_mode="bearer", access_token="t"
        )

    assert result.outcome == CheckInOutcome.ALREADY_CHECKED_IN
    assert result.business_code == 10001
    assert result.request_id == "rid-already"
    assert result.ok


@pytest.mark.asyncio
async def test_workbuddy_status_preflight_when_method_set():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        body = {"status": "CLAIMABLE"} if request.url.path.endswith("checkin-status") else {"code": 0}
        return _json_response(200, body, request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await WorkBuddyClient(status_method="GET", client=http).checkin(
            auth_mode="bearer", access_token="t"
        )

    assert result.outcome == CheckInOutcome.CLAIMED
    assert calls[0].startswith("GET ")
    assert any(call.startswith("POST ") for call in calls)


@pytest.mark.asyncio
async def test_workbuddy_status_already_short_circuits_claim():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return _json_response(200, {"status": "CLAIMED_TODAY"}, request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await WorkBuddyClient(status_method="GET", client=http).checkin(
            auth_mode="bearer", access_token="t"
        )

    assert result.outcome == CheckInOutcome.ALREADY_CHECKED_IN
    assert calls == ["GET"]


@pytest.mark.asyncio
async def test_workbuddy_status_never_falls_through_to_claim():
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return _json_response(200, {"status": "CLAIMABLE"}, request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await WorkBuddyClient(status_method="GET", client=http).status(
            auth_mode="bearer",
            access_token="t",
        )

    assert result.outcome == CheckInOutcome.FAILED
    assert calls == [("GET", "/billing/meter/checkin-status")]


@pytest.mark.asyncio
async def test_workbuddy_cookie_auth_mode():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        assert request.headers.get("Cookie") == "session=abc; other=1"
        return _json_response(200, {"code": 0}, request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await WorkBuddyClient(status_method="", client=http).checkin(
            auth_mode="cookie", cookie="session=abc; other=1"
        )
    assert result.outcome == CheckInOutcome.CLAIMED


@pytest.mark.asyncio
async def test_workbuddy_bearer_cookie_auth_mode():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer bt"
        assert request.headers.get("Cookie") == "c=1"
        return _json_response(200, {"code": 0}, request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await WorkBuddyClient(status_method="", client=http).checkin(
            auth_mode="bearer_cookie", access_token="bt", cookie="c=1"
        )
    assert result.outcome == CheckInOutcome.CLAIMED


@pytest.mark.asyncio
async def test_workbuddy_401_needs_reauth():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _json_response(401, {"msg": "unauthorized"}, request))
    ) as http:
        result = await WorkBuddyClient(status_method="", client=http).checkin(
            auth_mode="bearer", access_token="bad"
        )
    assert result.outcome == CheckInOutcome.NEEDS_REAUTH
    assert not result.ok


@pytest.mark.asyncio
async def test_workbuddy_missing_token_auth_failed():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request))
    ) as http:
        result = await WorkBuddyClient(status_method="", client=http).checkin(
            auth_mode="bearer", access_token=None
        )
    assert result.outcome == CheckInOutcome.AUTH_FAILED


@pytest.mark.asyncio
async def test_workbuddy_configurable_paths():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/billing/meter/daily-checkin"
        return _json_response(200, {"code": 0}, request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await WorkBuddyClient(
            base_url="https://www.workbuddy.cn",
            claim_path="/v2/billing/meter/daily-checkin",
            status_method="",
            client=http,
        ).checkin(auth_mode="bearer", access_token="t")
    assert result.outcome == CheckInOutcome.CLAIMED
