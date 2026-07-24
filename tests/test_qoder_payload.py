"""Qoder request payload mapping contracts."""

from qb2api.openai import ChatCompletionRequest
from qb2api.providers.qoder_payload import build_qoder_payload


def test_qoder_payload_preserves_message_roles_and_latest_user_context():
    request = ChatCompletionRequest(
        model="auto",
        messages=[
            {"role": "user", "content": "first"},
            {
                "role": "assistant",
                "content": "calling a tool",
                "tool_calls": [{"id": "call-1", "type": "function"}],
            },
            {"role": "tool", "content": "done", "tool_call_id": "call-1"},
            {"role": "user", "content": "latest"},
        ],
    )

    payload = build_qoder_payload(request, "auto")

    assert payload["model_config"] == {"key": "auto", "source": "system"}
    assert payload["chat_context"] == {
        "text": {"text": "latest"},
        "extra": {"originalContent": {"text": "latest"}},
    }
    assert payload["messages"][0]["contents"] == [{"type": "text", "text": "first"}]
    assert payload["messages"][1]["tool_calls"] == [{"id": "call-1", "type": "function"}]
    assert payload["messages"][2]["tool_call_id"] == "call-1"
    assert payload["messages"][3]["contents"] == [{"type": "text", "text": "latest"}]
