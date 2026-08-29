"""Shared validation and response helpers for observability routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import HTTPException, Request

from .dependencies import admin_state
from .validation import (
    choice_filter,
    optional_account_id,
    page_slice,
    provider_filter,
    text_filter,
    time_range,
)

_AUDIT_CATEGORIES = frozenset(
    {"account", "backup", "checkin", "credential", "metrics", "model", "proxy_key", "service", "settings", "usage"}
)


def repository(request: Request):
    selected = getattr(admin_state(request), "account_repo", None)
    if selected is None:
        raise HTTPException(status_code=503, detail="repository_unavailable")
    return selected


def usage_filters(request: Request) -> dict[str, str | None]:
    after, before = time_range(
        request.query_params.get("started_after"),
        request.query_params.get("started_before"),
    )
    return {
        "provider": provider_filter(request.query_params.get("provider")),
        "account_id": optional_account_id(request.query_params.get("account_id")),
        "model_id": text_filter(request.query_params.get("model_id"), detail="invalid_model_id"),
        "status": choice_filter(
            request.query_params.get("status"),
            {"succeeded", "failed"},
            detail="invalid_status",
        ),
        "started_after": after,
        "started_before": before,
    }


def audit_action_filters(
    action: str | None,
    action_prefix: str | None,
    category: str | None,
) -> tuple[str | None, str | None]:
    selected_action = text_filter(action, detail="invalid_action")
    selected_category = choice_filter(category, _AUDIT_CATEGORIES, detail="invalid_category")
    normalized_prefix = action_prefix.rstrip(".") if action_prefix else None
    selected_prefix = text_filter(
        normalized_prefix or selected_category,
        detail="invalid_action_prefix",
    )
    if selected_action and "." not in selected_action:
        choice_filter(selected_action, _AUDIT_CATEGORIES, detail="invalid_action")
        if selected_prefix and selected_prefix != selected_action:
            raise HTTPException(status_code=400, detail="conflicting_action_filter")
        return None, selected_action
    return selected_action, selected_prefix


def audit_search_filter(search: str | None, query: str | None) -> str | None:
    if search and query and search != query:
        raise HTTPException(status_code=400, detail="conflicting_search_filter")
    return text_filter(query or search, detail="invalid_search")


def page(
    key: str,
    *,
    values: list[dict[str, Any]],
    limit: int,
    offset: int,
) -> dict[str, Any]:
    selected, next_cursor = page_slice(values, offset, limit)
    return {key: selected, "limit": limit, "next_cursor": next_cursor}


def track_task(app: Any, task: asyncio.Task[Any]) -> None:
    tasks = getattr(app.state, "metrics_refresh_tasks", None)
    if tasks is None:
        tasks = set()
        app.state.metrics_refresh_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def safe_event(event: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "event_id", "request_id", "provider", "account_id", "model_id", "protocol",
        "status", "http_status", "input_tokens", "output_tokens", "latency_ms",
        "stream_committed", "started_at", "finished_at", "error_code",
        "reasoning_effort",
    )
    return {field: event.get(field) for field in fields}
