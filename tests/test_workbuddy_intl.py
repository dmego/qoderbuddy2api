"""International WorkBuddy: auth contract, message shaping, credit shape."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from fastapi import HTTPException

from qb2api.admin.import_support import intl_identity
from qb2api.admin.validation import label
from qb2api.admin.workbuddy_intl_routes import _identity_or
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
def _jwt(payload: dict) -> str:
    def enc(part: bytes) -> str:
        return base64.urlsafe_b64encode(part).decode().rstrip("=")

    return f"{enc(b'{}')}.{enc(json.dumps(payload).encode())}.sig"


class TestEmailLabels:
    def test_email_labels_are_accepted(self):
        assert label("muwnew@gmail.com", default="x") == "muwnew@gmail.com"
        assert label("foo+bar@x.cn", default="x") == "foo+bar@x.cn"

    def test_overlong_labels_are_rejected(self):
        with pytest.raises(HTTPException):
            label("a" * 65, default="x")


class TestIntlIdentity:
    def test_sub_and_prefers_email_then_handle_then_name(self):
        token = _jwt({"sub": "user-1", "email": "a@b.co", "preferred_username": "ab", "name": "A B"})
        assert intl_identity(token) == ("user-1", "a@b.co")

    def test_falls_back_to_handle_and_name(self):
        assert intl_identity(_jwt({"sub": "u", "preferred_username": "ab", "name": "A B"})) == ("u", "ab")
        assert intl_identity(_jwt({"sub": "u", "name": "A B"})) == ("u", "A B")

    def test_opaque_or_broken_tokens_yield_nones(self):
        assert intl_identity("plain-opaque-token") == (None, None)
        assert intl_identity("a.b.c") == (None, None)
        assert intl_identity("") == (None, None)

    def test_invalid_claim_values_are_skipped(self):
        assert intl_identity(_jwt({"sub": "u", "email": "x" * 80, "name": "A B"})) == ("u", "A B")
        assert intl_identity(_jwt({"sub": "u", "email": 123, "name": "A B"})) == ("u", "A B")
        assert intl_identity(_jwt({"email": "a@b.co"})) == (None, "a@b.co")


class TestIdentityOrDefault:
    def test_generic_label_becomes_token_identity(self):
        token = _jwt({"email": "muwnew@gmail.com"})
        assert _identity_or("WorkBuddy 国际版 OAuth", token) == "muwnew@gmail.com"
        assert _identity_or("workbuddy-intl", token) == "muwnew@gmail.com"

    def test_custom_label_is_kept(self):
        assert _identity_or("主账号", _jwt({"email": "muwnew@gmail.com"})) == "主账号"

    def test_generic_label_without_identity_is_kept(self):
        assert _identity_or("WorkBuddy 国际版 OAuth", "opaque") == "WorkBuddy 国际版 OAuth"
        assert _identity_or("workbuddy-intl", None) == "workbuddy-intl"
