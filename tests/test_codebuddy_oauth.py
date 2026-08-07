"""AUTH-01: CodeBuddy OAuth client + FlowStore (unverified refresh)."""

from __future__ import annotations

import hashlib
import time

import httpx
import pytest

from qb2api.auth.codebuddy_oauth import (
    AUTH_STATE_URL,
    AUTH_TOKEN_URL,
    CodeBuddyOAuthClient,
    CodeBuddyOAuthError,
)
from qb2api.auth.flows import FlowBusyError, FlowStore


def _handler_factory(routes: dict):
    """Build MockTransport handler from path-prefix -> callable(request)->Response."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for key, fn in routes.items():
            if key in url:
                return fn(request)
        return httpx.Response(404, json={"code": -1, "msg": "not found"})

    return handler


@pytest.mark.asyncio
async def test_oauth_start_returns_state_and_url():
    # AUTH-01: plugin/auth/state shape
    def on_state(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert "platform=CLI" in str(request.url)
        assert request.headers.get("User-Agent") == "CLI/1.0.8 CodeBuddy/1.0.8"
        assert request.headers.get("X-Product") == "SaaS"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "state": "state-abc",
                    "authUrl": "https://copilot.tencent.com/login?s=state-abc",
                },
            },
        )

    transport = httpx.MockTransport(_handler_factory({"/v2/plugin/auth/state": on_state}))
    async with httpx.AsyncClient(transport=transport) as http:
        client = CodeBuddyOAuthClient(http)
        result = await client.start()

    assert result.auth_state == "state-abc"
    assert "login" in result.auth_url
    assert AUTH_STATE_URL.endswith("/v2/plugin/auth/state")


@pytest.mark.asyncio
async def test_oauth_start_upstream_error():
    def on_state(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 50001, "msg": "upstream boom"})

    transport = httpx.MockTransport(_handler_factory({"/v2/plugin/auth/state": on_state}))
    async with httpx.AsyncClient(transport=transport) as http:
        client = CodeBuddyOAuthClient(http)
        with pytest.raises(CodeBuddyOAuthError):
            await client.start()


@pytest.mark.asyncio
async def test_oauth_poll_pending_11217():
    # AUTH-01: code=11217 means pending
    def on_token(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "state=pending-state" in str(request.url)
        assert request.headers.get("User-Agent") == "CLI/1.0.8 CodeBuddy/1.0.8"
        return httpx.Response(200, json={"code": 11217, "msg": "waiting"})

    transport = httpx.MockTransport(_handler_factory({"/v2/plugin/auth/token": on_token}))
    async with httpx.AsyncClient(transport=transport) as http:
        client = CodeBuddyOAuthClient(http)
        result = await client.poll("pending-state")

    assert result.status == "pending"
    assert result.code == 11217
    assert result.access_token is None


@pytest.mark.asyncio
async def test_oauth_poll_success_tokens():
    # AUTH-01: accessToken / optional refreshToken / expiresIn
    def on_token(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "accessToken": "at-secret",
                    "refreshToken": "rt-secret",
                    "expiresIn": 3600,
                    "tokenType": "Bearer",
                    "domain": "user@example.com",
                    "sessionState": "sess-1",
                },
            },
        )

    transport = httpx.MockTransport(_handler_factory({"/v2/plugin/auth/token": on_token}))
    async with httpx.AsyncClient(transport=transport) as http:
        client = CodeBuddyOAuthClient(http)
        result = await client.poll("ok-state")

    assert result.status == "success"
    assert result.access_token == "at-secret"
    assert result.refresh_token == "rt-secret"
    assert result.expires_in == 3600
    assert result.token_type == "Bearer"
    assert result.domain == "user@example.com"
    # redacted repr must not leak secrets
    text = repr(result)
    assert "at-secret" not in text
    assert "rt-secret" not in text


@pytest.mark.asyncio
async def test_oauth_poll_error_no_raw_body_in_message():
    secret_body = {"code": 40001, "msg": "bad", "data": {"leak": "should-not-surface"}}

    def on_token(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=secret_body)

    transport = httpx.MockTransport(_handler_factory({"/v2/plugin/auth/token": on_token}))
    async with httpx.AsyncClient(transport=transport) as http:
        client = CodeBuddyOAuthClient(http)
        result = await client.poll("err-state")

    assert result.status == "error"
    assert result.access_token is None
    assert "should-not-surface" not in (result.message or "")
    assert "leak" not in (result.message or "")


def test_flow_store_create_and_lookup():
    store = FlowStore(ttl_seconds=900)
    flow = store.create(label="alice", auth_state="raw-state-1", auth_url="https://auth.example/a")
    assert flow.flow_id
    assert flow.label == "alice"
    assert flow.auth_url.endswith("/a")
    assert flow.expires_at > time.time()
    # hashed state only on public record
    expected_hash = hashlib.sha256(b"raw-state-1").hexdigest()
    assert flow.state_hash == expected_hash
    assert "raw-state-1" not in flow.state_hash
    assert flow.state_hash != "raw-state-1"

    got = store.get(flow.flow_id)
    assert got is not None
    assert got.flow_id == flow.flow_id
    assert store.get_state(flow.flow_id) == "raw-state-1"


def test_flow_store_one_time_consume():
    store = FlowStore()
    flow = store.create(label="bob", auth_state="s2", auth_url="https://auth.example/b")
    assert store.consume(flow.flow_id) is True
    assert store.consume(flow.flow_id) is False
    assert store.get(flow.flow_id) is None
    assert store.get_state(flow.flow_id) is None


def test_flow_store_poll_lease_prevents_concurrent_consumers():
    store = FlowStore()
    flow = store.create(label="bob", auth_state="s2", auth_url="https://auth.example/b")

    lease = store.begin_poll(flow.flow_id)
    assert lease.auth_state == "s2"
    with pytest.raises(FlowBusyError):
        store.begin_poll(flow.flow_id)

    store.finish_poll(flow.flow_id, consume=False)
    store.begin_poll(flow.flow_id)
    store.finish_poll(flow.flow_id, consume=True)
    assert store.get(flow.flow_id) is None


def test_flow_store_ttl_expiry():
    store = FlowStore(ttl_seconds=1)
    flow = store.create(label="old", auth_state="s3", auth_url="https://auth.example/c")
    # force expire
    store._flows[flow.flow_id].record.expires_at = time.time() - 1
    assert store.get(flow.flow_id) is None
    assert store.get_state(flow.flow_id) is None
    assert store.consume(flow.flow_id) is False


def test_flow_store_unknown_id():
    store = FlowStore()
    assert store.get("missing") is None
    assert store.get_state("missing") is None
    assert store.consume("missing") is False


def test_auth_urls_match_reference():
    # AUTH-01 contract anchors
    assert AUTH_STATE_URL == "https://copilot.tencent.com/v2/plugin/auth/state"
    assert AUTH_TOKEN_URL == "https://copilot.tencent.com/v2/plugin/auth/token"
