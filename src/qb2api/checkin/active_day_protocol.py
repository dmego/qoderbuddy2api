"""Pure ACP message parsing helpers for the active-day client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any


class ActiveDayError(RuntimeError):
    """Safe, log-friendly active-day failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_sse_payload(lines: Iterable[str]) -> dict[str, Any] | None:
    data = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
    if not data.strip():
        return None
    try:
        payload = json.loads(data)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def handle_message(
    message: dict[str, Any] | None,
    pending: dict[int, asyncio.Future[dict[str, Any]]],
    turn_done: asyncio.Event,
) -> None:
    if not message:
        return
    response_id = message.get("id")
    future = pending.get(int(response_id)) if str(response_id).isdigit() else None
    if future is not None and not future.done():
        if isinstance(message.get("result"), dict):
            future.set_result(message["result"])
        elif isinstance(message.get("error"), dict):
            future.set_exception(ActiveDayError("rpc_error"))
    if is_end_turn(message):
        turn_done.set()


def is_end_turn(message: dict[str, Any]) -> bool:
    if message.get("method") in {"session_end_turn", "session/endTurn"}:
        return True
    params = message.get("params")
    if not isinstance(params, dict):
        return False
    update = params.get("update")
    return params.get("sessionUpdate") in {"session_end_turn", "session/endTurn"} or (
        isinstance(update, dict) and update.get("sessionUpdate") in {"session_end_turn", "session/endTurn"}
    )
