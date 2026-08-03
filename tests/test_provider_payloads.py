"""CodeBuddy and Qoder provider payload contracts."""

from qb2api.openai import ChatCompletionRequest
from qb2api.providers.codebuddy import CodeBuddyProvider, scrub_codebuddy_text
from qb2api.providers.qoder import QODER_CLI_MODEL_KEYS, QoderProvider, QoderSession


class TestCodeBuddyScrub:
    """CodeBuddy rejects Claude Code identity phrasing; scrub on outbound only."""

    def test_scrub_replaces_claude_code_system_prompt(self):
        source = "You are Claude Code, Anthropic's official CLI for Claude.\nHelp with code."

        result = scrub_codebuddy_text(source)

        assert result == "You are a helpful assistant."

    def test_scrub_replaces_sdk_and_agent_identity_variants(self):
        sdk = "You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK."
        agent = "You are a Claude agent, built on Anthropic's Claude Agent SDK."

        assert scrub_codebuddy_text(sdk) == "You are a helpful assistant."
        assert scrub_codebuddy_text(agent) == "You are a helpful assistant."

    def test_scrub_leaves_normal_system_untouched(self):
        source = "You are a helpful coding assistant."
        assert scrub_codebuddy_text(source) == source

    def test_build_body_scrubs_system_only(self):
        provider = CodeBuddyProvider(token="dummy")
        request = ChatCompletionRequest(
            model="hy3",
            messages=[
                {
                    "role": "system",
                    "content": "You are Claude Code, Anthropic's official CLI for Claude.\nBe concise.",
                },
                {
                    "role": "user",
                    "content": "You are Claude Code, Anthropic's official CLI for Claude.",
                },
            ],
        )

        body = provider._build_body(request)

        assert body["messages"][0]["content"] == "You are a helpful assistant."
        assert "Claude Code" in body["messages"][1]["content"]


class TestQoderToolCalls:
    """Test Qoder model mapping and COSY headers."""

    def test_qoder_maps_cli_display_model_to_internal_key(self):
        assert QODER_CLI_MODEL_KEYS["Qwen3.8-Max-Preview"] == "qmodel_preview"
        assert QODER_CLI_MODEL_KEYS["Qwen3.7-Max"] == "qmodel_latest"
        assert QODER_CLI_MODEL_KEYS["Kimi-K2.7-Code"] == "kmodel"
        assert QODER_CLI_MODEL_KEYS["DeepSeek-V4-Pro"] == "dmodel"

    def test_qoder_payload_uses_internal_key_for_named_model(self):
        provider = QoderProvider(pat="dummy")
        request = ChatCompletionRequest(
            model="Qwen3.7-Max",
            messages=[{"role": "user", "content": "hi"}],
        )

        payload = provider._build_payload(request, "Qwen3.7-Max")

        assert payload["model_config"] == {"key": "qmodel_latest", "source": "system"}

    def test_cosy_provider_has_no_cli_parser(self):
        assert not hasattr(QoderProvider(pat="dummy"), "_parse_tool_calls")

    def test_qoder_session_builds_headers(self):
        session = QoderSession(pat="dummy")
        session.user_id = "test-user"
        session.cosy_key = "test-key"
        session.payload_b64 = "test-payload"
        session._ready = True

        headers = session.chat_headers("test-body", "auto")

        assert "authorization" in headers
        assert headers["authorization"].startswith("Bearer COSY.")
        assert headers["cosy-version"] == "0.1.43"
