"""Tests for Anthropic Messages compatibility."""

import json
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient


class FakeProvider:
    name = "codebuddy"

    async def complete(self, request):
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 123,
            "model": request.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "hello",
                        "tool_calls": [
                            {
                                "id": "call_weather",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":"Tokyo"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18},
        }

    async def stream(self, request) -> AsyncIterator[bytes]:
        chunks = [
            {
                "choices": [
                    {
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"content": "hel"},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"content": "lo"},
                        "finish_reason": "stop",
                    }
                ]
            },
        ]
        for chunk in chunks:
            yield f"data: {json.dumps(chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    async def close(self) -> None:
        return None


def setup_fake_provider():
    from qb2api import app as app_module
    from qb2api.config import Settings
    from qb2api.logger import RequestLogger
    from qb2api.models import ModelDefinition

    app_module.registry.clear()
    app_module.registry.register(FakeProvider())
    app_module._model_index.clear()
    app_module._model_index["codebuddy"] = {"deepseek-v4-flash"}
    app_module.model_definitions = {
        "codebuddy": [
            ModelDefinition("deepseek-v4-flash", "DeepSeek V4 Flash", "codebuddy"),
        ]
    }
    app_module.settings = Settings()
    app_module.request_logger = RequestLogger(enabled=False)


def test_anthropic_request_converts_system_tools_and_tool_results():
    from qb2api.anthropic import anthropic_to_openai

    request = anthropic_to_openai(
        {
            "model": "codebuddy/deepseek-v4-flash",
            "system": "You are concise.",
            "max_tokens": 128,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "use tool result"},
                        {"type": "tool_result", "tool_use_id": "call_weather", "content": "sunny"},
                    ],
                }
            ],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get weather",
                    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
                }
            ],
            "tool_choice": {"type": "tool", "name": "get_weather"},
        }
    )

    assert request["messages"][0] == {"role": "system", "content": "You are concise."}
    assert request["messages"][1] == {"role": "user", "content": "use tool result"}
    assert request["messages"][2] == {"role": "tool", "tool_call_id": "call_weather", "content": "sunny"}
    assert request["tools"][0]["function"]["name"] == "get_weather"
    assert request["tool_choice"] == {"type": "function", "function": {"name": "get_weather"}}
    assert request["max_tokens"] == 128


def test_anthropic_response_converts_text_tools_and_usage():
    from qb2api.anthropic import openai_to_anthropic

    response = openai_to_anthropic(
        {
            "id": "chatcmpl-test",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "message": {
                        "content": "hello",
                        "tool_calls": [
                            {
                                "id": "call_weather",
                                "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18},
        },
        model="codebuddy/deepseek-v4-flash",
    )

    assert response["type"] == "message"
    assert response["role"] == "assistant"
    assert response["content"][0] == {"type": "text", "text": "hello"}
    assert response["content"][1] == {
        "type": "tool_use",
        "id": "call_weather",
        "name": "get_weather",
        "input": {"city": "Tokyo"},
    }
    assert response["stop_reason"] == "tool_use"
    assert response["usage"] == {"input_tokens": 7, "output_tokens": 11}


def test_v1_messages_returns_anthropic_message():
    from qb2api.app import app

    setup_fake_provider()
    client = TestClient(app)

    resp = client.post(
        "/v1/messages",
        json={
            "model": "codebuddy/deepseek-v4-flash",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "hello"
    assert body["usage"] == {"input_tokens": 7, "output_tokens": 11}


def test_v1_messages_streams_anthropic_events():
    from qb2api.app import app

    setup_fake_provider()
    client = TestClient(app)

    with client.stream(
        "POST",
        "/v1/messages",
        json={
            "model": "codebuddy/deepseek-v4-flash",
            "max_tokens": 64,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    ) as resp:
        text = resp.read().decode()

    assert resp.status_code == 200
    assert "event: message_start" in text
    assert '"type": "text_delta", "text": "hel"' in text
    assert '"stop_reason": "end_turn"' in text
    assert "event: message_stop" in text
