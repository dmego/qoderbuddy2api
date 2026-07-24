"""Check-in client tests (CB-CHECKIN-01 / QD-CHECKIN-01 reference contracts)."""

from __future__ import annotations

import json

import httpx
import pytest

from qb2api.checkin import (
    CheckInOutcome,
    QoderCheckinClient,
    WorkBuddyClient,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _json_response(status: int, body: dict | str | None, request: httpx.Request) -> httpx.Response:
    if body is None:
        content = b""
        headers = {}
    elif isinstance(body, str):
        content = body.encode()
        headers = {"content-type": "application/json"}
    else:
        content = json.dumps(body).encode()
        headers = {"content-type": "application/json"}
    return httpx.Response(status, content=content, headers=headers, request=request)


def _router(handlers: dict):
    """handlers: (method, path) -> (status, body) or callable(request)->Response."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method.upper(), request.url.path)
        if key not in handlers:
            return httpx.Response(404, json={"error": f"unhandled {key}"}, request=request)
        spec = handlers[key]
        if callable(spec):
            return spec(request)
        status, body = spec
        return _json_response(status, body, request)

    return handler


# ---------------------------------------------------------------------------
# WorkBuddy / CodeBuddy (CB-CHECKIN-01)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workbuddy_claim_success_without_status_preflight():
    """Empty status_method → skip status; POST claim 2xx → CLAIMED."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        assert request.headers.get("Authorization") == "Bearer tok-a"
        assert request.content == b"{}"
        return _json_response(200, {"code": 0, "msg": "ok", "requestId": "r1"}, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://www.workbuddy.cn") as http:
        client = WorkBuddyClient(
            base_url="https://www.workbuddy.cn",
            status_method="",  # skip preflight
            client=http,
        )
        result = await client.checkin(
            account_id="cb-1",
            auth_mode="bearer",
            access_token="tok-a",
        )

    assert result.outcome == CheckInOutcome.CLAIMED
    assert result.request_id == "r1"
    assert result.ok
    assert calls == [("POST", "/billing/meter/daily-checkin")]


@pytest.mark.asyncio
async def test_workbuddy_http_400_code_10001_is_already_checked_in():
    """Critical: HTTP 400 + code 10001 → ALREADY_CHECKED_IN, not FAILED."""

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

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = WorkBuddyClient(status_method="", client=http)
        result = await client.checkin(auth_mode="bearer", access_token="t")

    assert result.outcome == CheckInOutcome.ALREADY_CHECKED_IN
    assert result.business_code == 10001
    assert result.request_id == "rid-already"
    assert result.ok


@pytest.mark.asyncio
async def test_workbuddy_status_preflight_when_method_set():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("checkin-status"):
            return _json_response(200, {"status": "CLAIMABLE"}, request)
        return _json_response(200, {"code": 0}, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = WorkBuddyClient(status_method="GET", client=http)
        result = await client.checkin(auth_mode="bearer", access_token="t")

    assert result.outcome == CheckInOutcome.CLAIMED
    assert calls[0].startswith("GET ")
    assert any(c.startswith("POST ") for c in calls)


@pytest.mark.asyncio
async def test_workbuddy_status_already_short_circuits_claim():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return _json_response(200, {"status": "CLAIMED_TODAY"}, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = WorkBuddyClient(status_method="GET", client=http)
        result = await client.checkin(auth_mode="bearer", access_token="t")

    assert result.outcome == CheckInOutcome.ALREADY_CHECKED_IN
    assert calls == ["GET"]  # no claim


@pytest.mark.asyncio
async def test_workbuddy_cookie_auth_mode():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        assert request.headers.get("Cookie") == "session=abc; other=1"
        return _json_response(200, {"code": 0}, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = WorkBuddyClient(status_method="", client=http)
        result = await client.checkin(
            auth_mode="cookie",
            cookie="session=abc; other=1",
        )
    assert result.outcome == CheckInOutcome.CLAIMED


@pytest.mark.asyncio
async def test_workbuddy_bearer_cookie_auth_mode():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer bt"
        assert request.headers.get("Cookie") == "c=1"
        return _json_response(200, {"code": 0}, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = WorkBuddyClient(status_method="", client=http)
        result = await client.checkin(
            auth_mode="bearer_cookie",
            access_token="bt",
            cookie="c=1",
        )
    assert result.outcome == CheckInOutcome.CLAIMED


@pytest.mark.asyncio
async def test_workbuddy_401_needs_reauth():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(401, {"msg": "unauthorized"}, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = WorkBuddyClient(status_method="", client=http)
        result = await client.checkin(auth_mode="bearer", access_token="bad")
    assert result.outcome == CheckInOutcome.NEEDS_REAUTH
    assert not result.ok


@pytest.mark.asyncio
async def test_workbuddy_missing_token_auth_failed():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, request=r))
    async with httpx.AsyncClient(transport=transport) as http:
        client = WorkBuddyClient(status_method="", client=http)
        result = await client.checkin(auth_mode="bearer", access_token=None)
    assert result.outcome == CheckInOutcome.AUTH_FAILED


@pytest.mark.asyncio
async def test_workbuddy_configurable_paths():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/billing/meter/daily-checkin"
        return _json_response(200, {"code": 0}, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = WorkBuddyClient(
            base_url="https://www.workbuddy.cn",
            claim_path="/v2/billing/meter/daily-checkin",
            status_method="",
            client=http,
        )
        result = await client.checkin(auth_mode="bearer", access_token="t")
    assert result.outcome == CheckInOutcome.CLAIMED


# ---------------------------------------------------------------------------
# Qoder (QD-CHECKIN-01)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qoder_status_claimed_today():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/daily-check-in/status")
        assert request.headers.get("Authorization") == "Bearer acc"
        assert request.headers.get("User-Agent") == "QoderWork"
        return _json_response(
            200,
            {"status": "CLAIMED_TODAY", "rewardCredits": 10},
            request,
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = QoderCheckinClient(client=http)
        result = await client.status(access_token="acc", account_id="qd-1")

    assert result.outcome == CheckInOutcome.ALREADY_CHECKED_IN
    assert result.raw_status == "CLAIMED_TODAY"
    assert result.reward_credits == 10.0


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"status": "SOMETHING_NEW"}, {}, "not-json"])
async def test_qoder_unknown_success_status_is_not_verified(body):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, body, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = QoderCheckinClient(client=http)
        result = await client.status(access_token="acc", account_id="qd-1")

    assert result.outcome == CheckInOutcome.FAILED


@pytest.mark.asyncio
async def test_qoder_checkin_claimable_then_claim():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/status"):
            return _json_response(200, {"status": "CLAIMABLE"}, request)
        assert request.method == "POST"
        assert request.content == b"{}"
        return _json_response(200, {"result": "CLAIMED", "rewardCredits": 5}, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = QoderCheckinClient(client=http)
        result = await client.checkin(access_token="acc")

    assert result.outcome == CheckInOutcome.CLAIMED
    assert result.reward_credits == 5.0
    assert any("/status" in c for c in calls)
    assert any("/claim" in c for c in calls)


@pytest.mark.asyncio
async def test_qoder_claim_already_claimed():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"result": "ALREADY_CLAIMED"}, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = QoderCheckinClient(client=http)
        result = await client.claim(access_token="acc")

    assert result.outcome == CheckInOutcome.ALREADY_CHECKED_IN
    assert result.raw_status == "ALREADY_CLAIMED"


@pytest.mark.asyncio
async def test_qoder_claim_unknown_2xx_result_is_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"result": "NEW_UNKNOWN_STATE"}, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = QoderCheckinClient(client=http)
        result = await client.claim(access_token="acc")

    assert result.outcome == CheckInOutcome.FAILED
    assert not result.ok


@pytest.mark.asyncio
async def test_qoder_refresh_device_token_field():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/deviceToken/refresh")
        body = json.loads(request.content)
        assert body == {"refresh_token": "rt-old"}
        # no Authorization on refresh
        assert "Authorization" not in request.headers
        return _json_response(
            200,
            {"device_token": "new-access", "refresh_token": "rt-new"},
            request,
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = QoderCheckinClient(client=http)
        result = await client.refresh(refresh_token="rt-old")

    assert result.ok
    assert result.access_token == "new-access"
    assert result.refresh_token == "rt-new"


@pytest.mark.asyncio
async def test_qoder_refresh_token_field_fallback():
    """Accept `token` when `device_token` absent; keep refresh if not rotated."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"token": "access-via-token"}, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = QoderCheckinClient(client=http)
        result = await client.refresh(refresh_token="rt")

    assert result.access_token == "access-via-token"
    assert result.refresh_token is None  # caller keeps old refresh


@pytest.mark.asyncio
async def test_qoder_refresh_401_needs_reauth():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(401, {"msg": "invalid refresh"}, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = QoderCheckinClient(client=http)
        result = await client.refresh(refresh_token="bad")

    assert not result.ok
    assert result.outcome == CheckInOutcome.NEEDS_REAUTH


@pytest.mark.asyncio
async def test_qoder_status_401_needs_reauth():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(401, {}, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = QoderCheckinClient(client=http)
        result = await client.checkin(access_token="expired")

    assert result.outcome == CheckInOutcome.NEEDS_REAUTH


@pytest.mark.asyncio
async def test_qoder_rate_limited():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(429, {"msg": "slow down"}, request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = QoderCheckinClient(client=http)
        result = await client.claim(access_token="acc")

    assert result.outcome == CheckInOutcome.RATE_LIMITED


@pytest.mark.asyncio
async def test_outcomes_enum_complete():
    names = {o.name for o in CheckInOutcome}
    assert names == {
        "CLAIMED",
        "ALREADY_CHECKED_IN",
        "AUTH_FAILED",
        "NEEDS_REAUTH",
        "RATE_LIMITED",
        "TRANSIENT_ERROR",
        "FAILED",
        "SKIPPED",
    }
