"""Qoder check-in client contracts (QD-CHECKIN-01)."""

from __future__ import annotations

import json

import httpx
import pytest

from qb2api.checkin import CheckInOutcome, QoderCheckinClient


def _json_response(status: int, body: dict | str, request: httpx.Request) -> httpx.Response:
    content = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    return httpx.Response(status, content=content, headers={"content-type": "application/json"}, request=request)


@pytest.mark.asyncio
async def test_qoder_status_claimed_today():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path.endswith("/daily-check-in/status")
        assert request.headers.get("Authorization") == "Bearer acc"
        assert request.headers.get("User-Agent") == "QoderWork"
        return _json_response(200, {"status": "CLAIMED_TODAY", "rewardCredits": 10}, request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await QoderCheckinClient(client=http).status(access_token="acc", account_id="qd-1")

    assert result.outcome == CheckInOutcome.ALREADY_CHECKED_IN
    assert result.raw_status == "CLAIMED_TODAY"
    assert result.reward_credits == 10.0


@pytest.mark.asyncio
async def test_qoder_claim_extracts_reward_expiry():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: _json_response(
                200, {"result": "CLAIMED", "rewardCredits": 5, "expiresAt": 1787211510449}, request
            )
        )
    ) as http:
        result = await QoderCheckinClient(client=http).claim(access_token="acc")
    assert result.reward_expires_at == "2026-08-20T07:38:30.449000+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"status": "SOMETHING_NEW"}, {}, "not-json"])
async def test_qoder_unknown_success_status_is_not_verified(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _json_response(200, body, request))
    ) as http:
        result = await QoderCheckinClient(client=http).status(access_token="acc", account_id="qd-1")
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

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await QoderCheckinClient(client=http).checkin(access_token="acc")

    assert result.outcome == CheckInOutcome.CLAIMED
    assert result.reward_credits == 5.0
    assert any("/status" in call for call in calls)
    assert any("/claim" in call for call in calls)


@pytest.mark.asyncio
async def test_qoder_claim_already_claimed():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _json_response(200, {"result": "ALREADY_CLAIMED"}, request))
    ) as http:
        result = await QoderCheckinClient(client=http).claim(access_token="acc")
    assert result.outcome == CheckInOutcome.ALREADY_CHECKED_IN
    assert result.raw_status == "ALREADY_CLAIMED"


@pytest.mark.asyncio
async def test_qoder_claim_unknown_2xx_result_is_failed():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _json_response(200, {"result": "NEW_UNKNOWN_STATE"}, request))
    ) as http:
        result = await QoderCheckinClient(client=http).claim(access_token="acc")
    assert result.outcome == CheckInOutcome.FAILED
    assert not result.ok


@pytest.mark.asyncio
async def test_qoder_refresh_device_token_field():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/deviceToken/refresh")
        assert json.loads(request.content) == {"refresh_token": "rt-old"}
        assert "Authorization" not in request.headers
        return _json_response(200, {"device_token": "new-access", "refresh_token": "rt-new"}, request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await QoderCheckinClient(client=http).refresh(refresh_token="rt-old")
    assert result.ok
    assert result.access_token == "new-access"
    assert result.refresh_token == "rt-new"


@pytest.mark.asyncio
async def test_qoder_refresh_token_field_fallback():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _json_response(200, {"token": "access-via-token"}, request))
    ) as http:
        result = await QoderCheckinClient(client=http).refresh(refresh_token="rt")
    assert result.access_token == "access-via-token"
    assert result.refresh_token is None


@pytest.mark.asyncio
async def test_qoder_refresh_401_needs_reauth():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _json_response(401, {"msg": "invalid refresh"}, request))
    ) as http:
        result = await QoderCheckinClient(client=http).refresh(refresh_token="bad")
    assert not result.ok
    assert result.outcome == CheckInOutcome.NEEDS_REAUTH


@pytest.mark.asyncio
async def test_qoder_refresh_invalid_token_400_needs_reauth():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: _json_response(
                400,
                {
                    "errorCode": "BadRequest",
                    "errorMessage": "invalid refresh_token: must start with drt-",
                },
                request,
            )
        )
    ) as http:
        result = await QoderCheckinClient(client=http).refresh(refresh_token="jrt-old")

    assert result.outcome == CheckInOutcome.NEEDS_REAUTH
    assert result.message == "Qoder refresh credential rejected"


@pytest.mark.asyncio
async def test_qoder_status_401_needs_reauth():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _json_response(401, {}, request))
    ) as http:
        result = await QoderCheckinClient(client=http).checkin(access_token="expired")
    assert result.outcome == CheckInOutcome.NEEDS_REAUTH


@pytest.mark.asyncio
async def test_qoder_status_token_expire_400_needs_reauth():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: _json_response(
                400,
                {"code": "TOKEN_EXPIRE", "message": "token is not active"},
                request,
            )
        )
    ) as http:
        result = await QoderCheckinClient(client=http).checkin(access_token="expired")

    assert result.outcome == CheckInOutcome.NEEDS_REAUTH
    assert result.business_code == "TOKEN_EXPIRE"


@pytest.mark.asyncio
async def test_qoder_rate_limited():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: _json_response(429, {"msg": "slow down"}, request))
    ) as http:
        result = await QoderCheckinClient(client=http).claim(access_token="acc")
    assert result.outcome == CheckInOutcome.RATE_LIMITED


@pytest.mark.asyncio
async def test_outcomes_enum_complete():
    assert {outcome.name for outcome in CheckInOutcome} == {
        "CLAIMED", "ALREADY_CHECKED_IN", "AUTH_FAILED", "NEEDS_REAUTH",
        "RATE_LIMITED", "TRANSIENT_ERROR", "FAILED", "SKIPPED",
    }
