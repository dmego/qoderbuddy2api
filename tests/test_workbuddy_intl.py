"""International WorkBuddy: auth contract, message shaping, credit shape."""

from __future__ import annotations

import httpx
import pytest

from qb2api.auth.workbuddy_intl import (
    PENDING_CODE,
    WorkBuddyIntlAuthClient,
    WorkBuddyIntlAuthError,
)
from qb2api.checkin.codebuddy_credits import normalize_credits
from qb2api.openai import ChatCompletionRequest
from qb2api.providers.workbuddy_intl import (
    INTL_FALLBACK_SYSTEM_PROMPT,
    WorkBuddyIntlProvider,
    intl_messages,
)


def _request(messages: list[dict]) -> ChatCompletionRequest:
    return ChatCompletionRequest(model="hy3", messages=messages)


class TestIntlMessages:
    def test_user_first_gains_a_leading_system_message(self):
        shaped = intl_messages(_request([{"role": "user", "content": "hi"}]))
        assert shaped[0] == {"role": "system", "content": INTL_FALLBACK_SYSTEM_PROMPT}

    def test_multi_turn_user_first_still_gains_a_system_message(self):
        shaped = intl_messages(_request([
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]))
        assert [m["role"] for m in shaped] == ["system", "user", "assistant", "user"]

    def test_developer_role_folds_into_a_leading_system_message(self):
        shaped = intl_messages(_request([
            {"role": "developer", "content": "harness prompt"},
            {"role": "user", "content": "hi"},
        ]))
        assert shaped[0]["role"] == "system"
        assert shaped[0]["content"] == "harness prompt"

    def test_existing_system_message_is_kept_as_is(self):
        shaped = intl_messages(_request([
            {"role": "system", "content": "custom"},
            {"role": "user", "content": "hi"},
        ]))
        assert shaped[0]["content"] == "custom"
        assert len(shaped) == 2

    def test_client_messages_are_not_mutated(self):
        request = _request([{"role": "user", "content": "hi"}])
        intl_messages(request)
        assert [m.role for m in request.messages] == ["user"]


class TestIntlProviderBody:
    def test_body_always_carries_a_leading_system_message(self):
        provider = WorkBuddyIntlProvider(token="t")
        body = provider._build_body(_request([{"role": "user", "content": "hi"}]))
        assert body["messages"][0]["role"] == "system"
        assert body["stream"] is True
        assert body["model"] == "hy3"

    def test_default_reasoning_effort_is_injected_and_recorded(self):
        provider = WorkBuddyIntlProvider(token="t", default_reasoning_effort="low")
        request = _request([{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}])
        body = provider._build_body(request)
        assert body["reasoning_effort"] == "low"
        assert request.telemetry["reasoning_effort"] == "low"

    def test_client_supplied_effort_wins(self):
        provider = WorkBuddyIntlProvider(token="t", default_reasoning_effort="low")
        request = ChatCompletionRequest(
            model="hy3",
            messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}],
            reasoning_effort="high",
        )
        assert provider._build_body(request)["reasoning_effort"] == "high"

    def test_requires_token_or_getter(self):
        with pytest.raises(ValueError):
            WorkBuddyIntlProvider()

    @pytest.mark.asyncio
    async def test_headers_carry_authorization_and_intl_domain(self):
        provider = WorkBuddyIntlProvider(token="secret-token")
        headers = await provider._build_headers()
        assert headers["Authorization"] == "Bearer secret-token"
        assert headers["X-Domain"] == "www.workbuddy.ai"


def _client(handler) -> WorkBuddyIntlAuthClient:
    return WorkBuddyIntlAuthClient(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        base_url="https://www.workbuddy.ai",
    )


class TestIntlAuthClient:
    @pytest.mark.asyncio
    async def test_start_returns_state_and_auth_url(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v2/plugin/auth/state"
            assert request.url.params["platform"] == "CLI"
            return httpx.Response(200, json={
                "code": 0,
                "data": {"state": "st-1", "authUrl": "https://www.workbuddy.ai/login?state=st-1"},
            })

        client = _client(handler)
        started = await client.start()
        await client.aclose()
        assert started.auth_state == "st-1"
        assert started.auth_url.endswith("state=st-1")

    @pytest.mark.asyncio
    async def test_start_missing_fields_is_rejected(self):
        client = _client(lambda request: httpx.Response(200, json={"code": 0, "data": {}}))
        with pytest.raises(WorkBuddyIntlAuthError):
            await client.start()
        await client.aclose()

    @pytest.mark.asyncio
    async def test_poll_pending_code(self):
        client = _client(
            lambda request: httpx.Response(200, json={"code": PENDING_CODE, "msg": "login ing"})
        )
        result = await client.poll("st-1")
        await client.aclose()
        assert result.status == "pending"

    @pytest.mark.asyncio
    async def test_poll_success_exposes_tokens_only_on_fields(self):
        client = _client(lambda request: httpx.Response(200, json={
            "code": 0,
            "data": {
                "accessToken": "ACCESSSECRETVALUE",
                "refreshToken": "REFRESHSECRETVALUE",
                "expiresIn": 31534006,
                "domain": "www.workbuddy.ai",
            },
        }))
        result = await client.poll("st-1")
        await client.aclose()
        assert result.status == "success"
        assert result.access_token == "ACCESSSECRETVALUE"
        assert result.expires_in == 31534006
        assert "ACCESSSECRETVALUE" not in repr(result)
        assert "REFRESHSECRETVALUE" not in repr(result)

    @pytest.mark.asyncio
    async def test_refresh_uses_the_x_refresh_token_header(self):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v2/plugin/auth/token/refresh"
            seen.update(request.headers)
            return httpx.Response(200, json={
                "code": 0,
                "data": {"accessToken": "fresh", "refreshToken": "ref", "expiresIn": 10},
            })

        client = _client(handler)
        result = await client.refresh("ref-token")
        await client.aclose()
        assert result.status == "success"
        assert result.access_token == "fresh"
        assert seen["x-refresh-token"] == "ref-token"
        assert seen["x-auth-refresh-source"] == "plugin"

    @pytest.mark.asyncio
    async def test_refresh_without_a_token_short_circuits(self):
        client = _client(lambda request: httpx.Response(500))
        result = await client.refresh("")
        await client.aclose()
        assert result.status == "error"

    @pytest.mark.asyncio
    async def test_poll_error_body_is_not_echoed_raw(self):
        client = _client(lambda request: httpx.Response(200, json={
            "code": 40001,
            "msg": "denied",
        }))
        result = await client.poll("st-1")
        await client.aclose()
        assert result.status == "error"
        assert result.code == 40001


class TestIntlCreditsShape:
    def test_intl_credit_payload_normalizes_like_the_domestic_one(self):
        body = {
            "code": 0,
            "data": {
                "Response": {
                    "Data": {
                        "Accounts": [
                            {
                                "CapacityUnit": "credit",
                                "CapacityRemain": 250,
                                "CapacityUsed": 0,
                                "CapacitySize": 250,
                                "CycleCapacityRemain": 250,
                                "CycleCapacitySize": 250,
                                "PackageName": "Bonus Pack",
                                "CycleEndTime": "2026-09-25 00:50:13",
                            }
                        ]
                    }
                }
            },
        }
        value = normalize_credits(body)
        assert value["total_remaining"] == 250
        assert value["total_capacity"] == 250
        assert value["unit"] == "credit"
        assert value["packages"][0]["name"] == "Bonus Pack"

    def test_error_payload_yields_no_snapshot(self):
        assert normalize_credits({"code": 10001, "msg": "nope"}) == {}
        assert normalize_credits(None) == {}
