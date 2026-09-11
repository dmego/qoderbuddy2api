"""Protocol-level regression tests for Worker-owned proxy routes."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient

from qb2api.config import Settings
from qb2api.models import ModelDefinition
from qb2api.openai import ChatCompletionRequest
from qb2api.worker.app import create_worker_app


class _FakeProvider:
    name = "test"

    async def complete(self, request: ChatCompletionRequest) -> dict:
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 123,
            "model": request.model,
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }

    async def stream(self, request: ChatCompletionRequest) -> AsyncIterator[bytes]:
        chunk = {"choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    async def close(self) -> None:
        return None


@pytest.fixture
def worker_client() -> Iterator[TestClient]:
    application = create_worker_app(lambda: Settings(codebuddy_tokens=["ck-worker"]))
    with TestClient(application) as client:
        state = client.app.state.proxy_state
        state.registry.clear()
        state.registry.register(_FakeProvider())
        state.model_definitions = {"test": [ModelDefinition("echo", "Echo", "test")]}
        state._rebuild_catalog()
        yield client


def test_worker_serves_openai_completion_with_its_own_provider_state(worker_client: TestClient) -> None:
    response = worker_client.post(
        "/v1/chat/completions",
        json={"model": "test/echo", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hello"


def test_worker_converts_anthropic_messages_with_its_own_provider_state(worker_client: TestClient) -> None:
    response = worker_client.post(
        "/v1/messages",
        json={"model": "test/echo", "max_tokens": 32, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert response.json()["content"] == [{"type": "text", "text": "hello"}]


def test_worker_streams_openai_events_without_a_legacy_subapplication(worker_client: TestClient) -> None:
    with worker_client.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "test/echo", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as response:
        body = response.read()

    assert response.status_code == 200
    assert b'"content": "hello"' in body
    assert b"data: [DONE]" in body


def test_worker_lists_models_from_its_own_provider_state(worker_client: TestClient) -> None:
    response = worker_client.get("/v1/models")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == ["echo"]


def test_worker_reports_active_quota_blocks(worker_client: TestClient) -> None:
    import time as time_module

    runtime = worker_client.app.state.runtime
    pool = runtime.codebuddy_pool
    pool._model_blocked[("codebuddy:cb-worker-0", "echo")] = time_module.monotonic() + 120

    response = worker_client.get("/internal/quota-blocks")

    assert response.status_code == 200
    blocks = response.json()["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["provider"] == "codebuddy"
    assert blocks[0]["account_id"] == "cb-worker-0"
    assert blocks[0]["model_id"] == "echo"
    assert blocks[0]["blocked_until"].endswith("Z")


def test_worker_quota_blocks_cover_every_provider_pool(worker_client: TestClient) -> None:
    """The intl pool carries quota blocks too; a provider missing from this
    endpoint would silently hide its blocked accounts from the admin UI."""
    import time as time_module

    runtime = worker_client.app.state.runtime
    now = time_module.monotonic() + 120
    runtime.codebuddy_pool._model_blocked[("codebuddy:cb-worker-0", "echo")] = now
    runtime.workbuddy_intl_pool._model_blocked[("workbuddy_intl:wbintl-1", "hy3")] = now

    response = worker_client.get("/internal/quota-blocks")

    assert response.status_code == 200
    providers = {block["provider"] for block in response.json()["blocks"]}
    assert providers == {"codebuddy", "workbuddy_intl"}
