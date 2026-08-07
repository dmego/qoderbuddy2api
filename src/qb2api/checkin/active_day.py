"""WorkBuddy ACP active-day client."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .active_day_protocol import ActiveDayError, handle_message
from .active_day_protocol import parse_sse_payload as _parse_sse_payload
from .base import join_url

_PROMPT = "你好"
_MODEL = "hy3"


class WorkBuddyActiveDayClient:
    """Record one WorkBuddy growth-center active day through ACP."""

    def __init__(
        self,
        *,
        base_url: str = "https://copilot.tencent.com",
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0))
        )
        self._connection_id: str | None = None
        self._acp_session_token: str | None = None
        self._link: str | None = None
        self._session_token: str | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._stream_tasks: set[asyncio.Task[None]] = set()
        self._get_context: Any = None
        self._turn_done = asyncio.Event()
        self._stream_error: ActiveDayError | None = None

    async def aclose(self) -> None:
        await self._disconnect()
        if self._owns_client:
            await self._client.aclose()

    async def run(self, access_token: str) -> None:
        if not access_token or not access_token.strip():
            raise ActiveDayError("access_token_missing")
        try:
            await self._run_protocol(access_token)
        except ActiveDayError:
            raise
        except TimeoutError as error:
            raise ActiveDayError("rpc_timeout") from error
        except httpx.HTTPError as error:
            raise ActiveDayError(f"transport:{type(error).__name__}") from error
        except (OSError, RuntimeError) as error:
            raise ActiveDayError(f"rpc_failed:{type(error).__name__}") from error
        finally:
            await self._disconnect()

    async def _run_protocol(self, access_token: str) -> None:
        conversation_id = await self._create_conversation(access_token)
        session = await self._session_info(access_token, conversation_id)
        self._link = session.get("link")
        if not isinstance(self._link, str) or not self._link:
            raise ActiveDayError("session_info_missing")
        token = session.get("token")
        self._session_token = token if isinstance(token, str) and token.strip() else access_token
        await self._open_sse()
        await self._rpc(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "qb2api", "version": "1"},
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}},
            },
        )
        session_result = await self._rpc(
            "session/new", {"cwd": session.get("cwd") or "/workspace", "mcpServers": []}
        )
        session_id = session_result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise ActiveDayError("session_new_missing")
        await self._rpc("session/set_model", {"sessionId": session_id, "modelId": _MODEL})
        self._turn_done.clear()
        await self._rpc(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": _PROMPT}]},
            wait_response=False,
        )
        await asyncio.wait_for(self._turn_done.wait(), timeout=self.timeout)
        if self._stream_error:
            raise self._stream_error

    async def _create_conversation(self, access_token: str) -> str:
        body = {
            "prompt": _PROMPT,
            "model": _MODEL,
            "plugins": [{"name": "weixinpay", "marketplace": "codebuddy-builtin"}],
        }
        payload = await self._request_json(
            "POST", join_url(self.base_url, "/console/as/conversations/"), access_token, json_body=body
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        conversation_id = data.get("id") or data.get("conversationId")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ActiveDayError("conversation_missing")
        return conversation_id

    async def _session_info(self, access_token: str, conversation_id: str) -> dict[str, Any]:
        payload = await self._request_json(
            "GET",
            join_url(self.base_url, f"/console/as/conversations/{conversation_id}/session"),
            access_token,
        )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        return data if isinstance(data, dict) else {}

    async def _request_json(
        self, method: str, url: str, access_token: str, *, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await self._client.request(
            method, url, headers=self._headers(access_token), json=json_body, timeout=self.timeout
        )
        if not 200 <= response.status_code < 300:
            raise ActiveDayError(f"http:{response.status_code}")
        try:
            payload = response.json()
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ActiveDayError("invalid_json") from error
        if not isinstance(payload, dict):
            raise ActiveDayError("invalid_json")
        return payload

    async def _open_sse(self) -> None:
        assert self._link is not None
        context = self._client.stream("GET", self._link, headers=self._acp_headers())
        response = await context.__aenter__()
        if not 200 <= response.status_code < 300:
            await context.__aexit__(None, None, None)
            raise ActiveDayError(f"acp_sse_http:{response.status_code}")
        connection_id = response.headers.get("Acp-Connection-Id")
        if not connection_id:
            await context.__aexit__(None, None, None)
            raise ActiveDayError("acp_connection_missing")
        self._connection_id = connection_id
        self._acp_session_token = response.headers.get("acp-session-token")
        self._get_context = context
        self._track_stream(asyncio.create_task(self._consume(response)))

    async def _rpc(
        self, method: str, params: dict[str, Any], *, wait_response: bool = True
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        if not wait_response:
            await self._post_rpc({
                "jsonrpc": "2.0", "id": request_id, "method": method, "params": params
            })
            return {}
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            await self._post_rpc(message)
            return await asyncio.wait_for(asyncio.shield(future), timeout=self.timeout)
        except TimeoutError as error:
            raise ActiveDayError("rpc_timeout") from error
        finally:
            self._pending.pop(request_id, None)

    async def _post_rpc(self, message: dict[str, Any]) -> None:
        assert self._link is not None
        context = self._client.stream("POST", self._link, headers=self._acp_headers(), json=message)
        response = await context.__aenter__()
        if not 200 <= response.status_code < 300:
            await context.__aexit__(None, None, None)
            raise ActiveDayError(f"acp_rpc_http:{response.status_code}")
        content_type = response.headers.get("content-type", "").lower()
        if "text/event-stream" in content_type:
            self._track_stream(asyncio.create_task(self._consume_and_close(response, context)))
            return
        try:
            raw = await response.aread()
            if raw.strip():
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ActiveDayError("invalid_json") from error
                if isinstance(payload, dict):
                    handle_message(payload, self._pending, self._turn_done)
        finally:
            await context.__aexit__(None, None, None)

    async def _consume_and_close(self, response: httpx.Response, context: Any) -> None:
        try:
            await self._consume(response)
        finally:
            await context.__aexit__(None, None, None)

    async def _consume(self, response: httpx.Response) -> None:
        event_lines: list[str] = []
        async for line in response.aiter_lines():
            if line:
                event_lines.append(line)
                continue
            message = _parse_sse_payload(event_lines)
            handle_message(message, self._pending, self._turn_done)
            event_lines = []
        if event_lines:
            handle_message(_parse_sse_payload(event_lines), self._pending, self._turn_done)

    def _track_stream(self, task: asyncio.Task[None]) -> None:
        self._stream_tasks.add(task)
        task.add_done_callback(self._stream_finished)

    def _stream_finished(self, task: asyncio.Task[None]) -> None:
        self._stream_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        self._stream_error = error if isinstance(error, ActiveDayError) else ActiveDayError("stream_failed")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(self._stream_error)
        self._turn_done.set()

    def _headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        }

    def _acp_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._session_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "x-codebuddy-request": "1",
        }
        if self._connection_id:
            headers["Acp-Connection-Id"] = self._connection_id
        if self._acp_session_token:
            headers["acp-session-token"] = self._acp_session_token
        return headers

    async def _disconnect(self) -> None:
        for task in list(self._stream_tasks):
            task.cancel()
        if self._stream_tasks:
            await asyncio.gather(*self._stream_tasks, return_exceptions=True)
        self._stream_tasks.clear()
        if self._get_context is not None:
            await self._get_context.__aexit__(None, None, None)
            self._get_context = None
        if self._link and self._connection_id:
            try:
                await self._client.request(
                    "DELETE", self._link, headers=self._acp_headers(), timeout=min(self.timeout, 5.0)
                )
            except httpx.HTTPError:
                pass
        self._link = None
        self._connection_id = None
        self._acp_session_token = None
        self._session_token = None
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()
        self._stream_error = None
