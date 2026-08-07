"""WorkBuddy ACP active-day contracts."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from qb2api.checkin.active_day import ActiveDayError, WorkBuddyActiveDayClient, _parse_sse_payload


def test_parse_sse_payload_reads_json_data_lines() -> None:
    assert _parse_sse_payload(["event: message", "data: {\"id\": 1}", ""]) == {"id": 1}


class _ActiveDayStream(httpx.AsyncByteStream):
    def __init__(self, prompt_sent: asyncio.Event) -> None:
        self.prompt_sent = prompt_sent

    def __aiter__(self):
        return self._events()

    async def _events(self):
        await self.prompt_sent.wait()
        payload = (
            b"event: message\ndata: {\"jsonrpc\":\"2.0\","
            b"\"method\":\"session/update\",\"params\":{"
            b"\"update\":{\"sessionUpdate\":\"session_end_turn\"}}}\n\n"
        )
        yield payload[:30]
        yield payload[30:]


class _FailingActiveDayStream(httpx.AsyncByteStream):
    def __init__(self, prompt_sent: asyncio.Event) -> None:
        self.prompt_sent = prompt_sent

    def __aiter__(self):
        return self._events()

    async def _events(self):
        await self.prompt_sent.wait()
        if False:
            yield b""
        raise RuntimeError("upstream stream failed")


@pytest.mark.asyncio
async def test_active_day_uses_streamable_http_acp_flow() -> None:
    prompt_sent = asyncio.Event()
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/console/as/conversations/":
            return httpx.Response(200, json={"id": "conversation-1"}, request=request)
        if request.url.host == "copilot.test" and request.url.path.endswith("/session"):
            return httpx.Response(
                200,
                json={"link": "https://acp.test/session", "token": "session-token"},
                request=request,
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={
                    "Acp-Connection-Id": "connection-1",
                    "acp-session-token": "transport-token",
                    "Content-Type": "text/event-stream",
                },
                stream=_ActiveDayStream(prompt_sent),
                request=request,
            )
        if request.method == "DELETE":
            return httpx.Response(200, request=request)
        body = json.loads(request.content)
        if body["method"] == "session/prompt":
            prompt_sent.set()
            return httpx.Response(202, request=request)
        result = {"sessionId": "session-1"} if body["method"] == "session/new" else {}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        await WorkBuddyActiveDayClient(base_url="https://copilot.test", client=http, timeout=1).run("access-token")

    methods = [
        json.loads(item.content).get("method")
        for item in requests
        if item.method == "POST" and item.url.host == "acp.test"
    ]
    assert methods == ["initialize", "session/new", "session/set_model", "session/prompt"]
    assert requests[-1].method == "DELETE"
    assert all(
        item.headers.get("Authorization") == "Bearer session-token"
        for item in requests
        if item.url.host == "acp.test"
    )
    assert all(
        item.headers.get("acp-session-token") == "transport-token"
        for item in requests
        if item.method == "POST" and item.url.host == "acp.test"
    )


@pytest.mark.asyncio
async def test_active_day_surfaces_background_stream_failure() -> None:
    prompt_sent = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/console/as/conversations/":
            return httpx.Response(200, json={"id": "conversation-1"}, request=request)
        if request.url.host == "copilot.test" and request.url.path.endswith("/session"):
            return httpx.Response(200, json={"link": "https://acp.test/session"}, request=request)
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={"Acp-Connection-Id": "connection-1", "Content-Type": "text/event-stream"},
                stream=_FailingActiveDayStream(prompt_sent),
                request=request,
            )
        if request.method == "DELETE":
            return httpx.Response(200, request=request)
        body = json.loads(request.content)
        if body["method"] == "session/prompt":
            prompt_sent.set()
            return httpx.Response(202, request=request)
        result = {"sessionId": "session-1"} if body["method"] == "session/new" else {}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result}, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(ActiveDayError, match="stream_failed"):
            await WorkBuddyActiveDayClient(base_url="https://copilot.test", client=http, timeout=1).run("access-token")
