"""Security and failure-isolation contracts added by the provider split."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import httpx
import pytest

from qb2api.checkin import CheckInOutcome, QoderCheckinClient, WorkBuddyClient
from qb2api.openai import ChatCompletionRequest
from qb2api.providers.qoder import QoderError, QoderProvider, QoderSession


def _json_response(
    status: int,
    body: dict,
    request: httpx.Request,
) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        request=request,
    )


@pytest.mark.asyncio
async def test_workbuddy_shared_client_does_not_leak_cookies_between_accounts():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert request.headers.get("Cookie") == "session=account-a"
            return httpx.Response(
                200,
                json={"code": 0},
                headers={"set-cookie": "leaked=account-a; Path=/"},
                request=request,
            )
        assert request.headers.get("Authorization") == "Bearer account-b"
        assert "Cookie" not in request.headers
        return _json_response(200, {"code": 0}, request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = WorkBuddyClient(status_method="", client=http)
        first = await client.checkin(
            account_id="account-a",
            auth_mode="cookie",
            cookie="session=account-a",
        )
        second = await client.checkin(
            account_id="account-b",
            auth_mode="bearer",
            access_token="account-b",
        )

    assert first.outcome == CheckInOutcome.CLAIMED
    assert second.outcome == CheckInOutcome.CLAIMED


@pytest.mark.asyncio
async def test_workbuddy_rejects_unknown_auth_mode_without_request():
    transport = httpx.MockTransport(
        lambda request: pytest.fail(f"unexpected request: {request.url}")
    )
    async with httpx.AsyncClient(transport=transport) as http:
        client = WorkBuddyClient(status_method="", client=http)
        result = await client.checkin(
            account_id="cb-invalid",
            auth_mode="inherit_chat",  # type: ignore[arg-type]
            access_token="must-not-be-used",
        )

    assert result.outcome == CheckInOutcome.AUTH_FAILED
    assert result.message == "unsupported auth_mode: inherit_chat"


@pytest.mark.asyncio
async def test_workbuddy_only_accepts_identity_extra_headers():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer expected"
        assert request.headers["X-User-Id"] == "user-1"
        assert request.headers["Host"] == "www.workbuddy.cn"
        return _json_response(200, {"code": 0}, request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = WorkBuddyClient(status_method="", client=http)
        result = await client.checkin(
            auth_mode="bearer",
            access_token="expected",
            extra_headers={
                "Authorization": "Bearer injected",
                "Cookie": "injected=1",
                "Host": "attacker.invalid",
                "X-User-Id": "user-1",
            },
        )

    assert result.outcome == CheckInOutcome.CLAIMED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, CheckInOutcome.RATE_LIMITED),
        (503, CheckInOutcome.TRANSIENT_ERROR),
    ],
)
async def test_qoder_refresh_preserves_retryable_error_classification(status, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(status, {"msg": "temporary"}, request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await QoderCheckinClient(client=http).refresh(refresh_token="refresh")

    assert result.outcome == expected


@pytest.mark.asyncio
async def test_qoder_disabled_status_is_not_claimed():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return _json_response(200, {"status": "DISABLED"}, request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await QoderCheckinClient(client=http).checkin(access_token="access")

    assert result.outcome == CheckInOutcome.FAILED
    assert result.raw_status == "DISABLED"
    assert calls == ["GET"]


@pytest.mark.asyncio
async def test_concurrent_qoder_401_rebuilds_session_once(monkeypatch):
    sessions: list[QoderSession] = []
    failures = 0
    both_failed = asyncio.Event()

    async def authenticate(session: QoderSession) -> None:
        session._ready = True
        sessions.append(session)

    async def stream_once(
        _provider: QoderProvider,
        _request: ChatCompletionRequest,
        session: QoderSession,
    ) -> AsyncIterator[bytes]:
        nonlocal failures
        if session is sessions[0]:
            failures += 1
            if failures == 2:
                both_failed.set()
            await both_failed.wait()
            raise QoderError("expired", status_code=401)
        yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'

    monkeypatch.setattr(QoderSession, "authenticate", authenticate)
    monkeypatch.setattr(QoderProvider, "_stream_once", stream_once)
    provider = QoderProvider(pat="pat-a")
    request = ChatCompletionRequest(
        model="auto",
        messages=[{"role": "user", "content": "hi"}],
    )

    async def consume() -> list[bytes]:
        return [chunk async for chunk in provider.stream(request)]

    try:
        results = await asyncio.gather(consume(), consume())
    finally:
        await provider.close()

    assert len(sessions) == 2
    assert all(chunks[-1] == b"data: [DONE]\n\n" for chunks in results)


def _exporter_module():
    path = Path(__file__).parents[1] / "tools/qoder-checkin-exporter/export_stub.py"
    spec = spec_from_file_location("qoder_checkin_exporter", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_qoder_exporter_emits_minimal_import_schema():
    payload = _exporter_module().build_payload(
        access_token="device-access-token",
        refresh_token="device-refresh-token",
        expires_at="2026-07-25T00:00:00Z",
        account_hint="qoder-main",
    )

    assert payload == {
        "version": 1,
        "provider": "qoder",
        "account_hint": "qoder-main",
        "access_token": "device-access-token",
        "refresh_token": "device-refresh-token",
        "expires_at": "2026-07-25T00:00:00Z",
    }


@pytest.mark.parametrize(
    ("access_token", "refresh_token"),
    [
        ("pt_chat-only", "refresh-token"),
        ("Bearer COSY.payload.signature", "refresh-token"),
        ("device-access", ""),
    ],
)
def test_qoder_exporter_rejects_unsafe_or_incomplete_credentials(
    access_token,
    refresh_token,
):
    with pytest.raises(ValueError):
        _exporter_module().build_payload(
            access_token=access_token,
            refresh_token=refresh_token,
        )
