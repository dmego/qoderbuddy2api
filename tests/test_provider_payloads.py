"""CodeBuddy and Qoder provider payload contracts."""

from qb2api.openai import ChatCompletionRequest
from qb2api.providers.codebuddy import CodeBuddyProvider
from qb2api.providers.codebuddy_scrub import scrub_codebuddy_text
from qb2api.providers.qoder import QODER_CLI_MODEL_KEYS, QoderProvider, QoderSession


class TestCodeBuddyUpstreamError:
    """Typed handling of upstream error bodies (11128 channel ban, 6004 model quota)."""

    def test_11128_parses_to_channel_blocked_with_display_message(self):
        from qb2api.providers.codebuddy import (
            CodeBuddyChannelBlockedError,
            parse_codebuddy_error,
        )

        body = (
            '{"code":11128,"msg":"Illegal API invocation from an unapproved channel",'
            '"requestId":"x","displayMsg":{"en":"The request was blocked by security policy. Please retry"}}'
        )
        error = parse_codebuddy_error(400, body)
        assert isinstance(error, CodeBuddyChannelBlockedError)
        assert error.status_code == 400
        assert "upstream security policy; back off and retry later" in str(error)
        assert "Please retry" in str(error)

    def test_11128_without_displayMsg_falls_back_to_msg(self):
        from qb2api.providers.codebuddy import (
            CodeBuddyChannelBlockedError,
            parse_codebuddy_error,
        )

        error = parse_codebuddy_error(400, '{"code":11128,"msg":"blocked"}')
        assert isinstance(error, CodeBuddyChannelBlockedError)
        assert "blocked" in str(error)

    def test_non_11128_is_generic_error(self):
        from qb2api.providers.codebuddy import (
            CodeBuddyChannelBlockedError,
            CodeBuddyError,
            parse_codebuddy_error,
        )

        error = parse_codebuddy_error(400, '{"code":11102,"msg":"model service info not found"}')
        assert isinstance(error, CodeBuddyError)
        assert not isinstance(error, CodeBuddyChannelBlockedError)
        assert "model service info not found" in str(error)

    def test_non_json_preserves_raw_prefix(self):
        from qb2api.providers.codebuddy import CodeBuddyError, parse_codebuddy_error

        error = parse_codebuddy_error(500, "boom\nline")
        assert isinstance(error, CodeBuddyError)
        assert str(error) == "CodeBuddy 500: boom line"

    def test_6004_parses_to_quota_exceeded_with_reset_at(self):
        from datetime import UTC, datetime

        from qb2api.providers.codebuddy import (
            CodeBuddyQuotaExceededError,
            parse_codebuddy_error,
        )

        body = (
            '{"code":6004,"msg":"您的使用量已超出频率限制，将在 2026-09-11 17:53:33 '
            'UTC+8 重置，您也可以切换其他模型继续使用。","requestId":"fa883b18"}'
        )
        error = parse_codebuddy_error(429, body)
        assert isinstance(error, CodeBuddyQuotaExceededError)
        assert error.status_code == 429
        assert error.model_block_reset_at == datetime(2026, 9, 11, 9, 53, 33, tzinfo=UTC)
        assert "切换其他模型" in str(error)

    def test_6004_without_reset_time_yields_none(self):
        from qb2api.providers.codebuddy import CodeBuddyQuotaExceededError, parse_codebuddy_error

        error = parse_codebuddy_error(429, '{"code":6004,"msg":"您的使用量已超出频率限制。"}')
        assert isinstance(error, CodeBuddyQuotaExceededError)
        assert error.model_block_reset_at is None


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

    def test_build_body_injects_default_effort_when_absent(self):
        provider = CodeBuddyProvider(token="dummy", default_reasoning_effort="high")
        request = ChatCompletionRequest(
            model="hy3",
            messages=[{"role": "user", "content": "hi"}],
        )

        body = provider._build_body(request)

        assert body["reasoning_effort"] == "high"
        assert request.telemetry["reasoning_effort"] == "high"

    def test_build_body_respects_client_supplied_effort(self):
        provider = CodeBuddyProvider(token="dummy", default_reasoning_effort="high")
        request = ChatCompletionRequest(
            model="hy3",
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="low",
        )

        body = provider._build_body(request)

        assert body["reasoning_effort"] == "low"
        assert request.telemetry["reasoning_effort"] == "low"

    def test_build_body_no_effort_when_default_empty(self):
        provider = CodeBuddyProvider(token="dummy", default_reasoning_effort="")
        request = ChatCompletionRequest(
            model="hy3",
            messages=[{"role": "user", "content": "hi"}],
        )

        body = provider._build_body(request)

        assert "reasoning_effort" not in body


class TestCodeBuddyDeveloperRole:
    """Upstream 11128-rejects any ``developer`` message; fold it into ``system``."""

    def test_build_body_folds_developer_role_into_system(self):
        provider = CodeBuddyProvider(token="dummy")
        request = ChatCompletionRequest(
            model="deepseek-v4.1-flash",
            messages=[
                {"role": "developer", "content": "You are an AI agent powered by DeepSeek Harness."},
                {"role": "user", "content": "hi"},
            ],
        )

        body = provider._build_body(request)

        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert body["messages"][0]["content"] == "You are an AI agent powered by DeepSeek Harness."

    def test_build_body_keeps_non_developer_roles_unchanged(self):
        provider = CodeBuddyProvider(token="dummy")
        request = ChatCompletionRequest(
            model="deepseek-v4.1-flash",
            messages=[
                {"role": "system", "content": "sys"},
                {"role": "developer", "content": "dev"},
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
        )

        body = provider._build_body(request)

        assert [m["role"] for m in body["messages"]] == ["system", "system", "user", "assistant"]

    def test_build_body_scrubs_claude_prompt_on_developer_role(self):
        provider = CodeBuddyProvider(token="dummy")
        request = ChatCompletionRequest(
            model="deepseek-v4.1-flash",
            messages=[
                {
                    "role": "developer",
                    "content": "You are Claude Code, Anthropic's official CLI for Claude.",
                },
                {"role": "user", "content": "hi"},
            ],
        )

        body = provider._build_body(request)

        assert body["messages"][0]["role"] == "system"
        assert body["messages"][0]["content"] == "You are a helpful assistant."

    def test_build_body_does_not_mutate_client_messages(self):
        provider = CodeBuddyProvider(token="dummy")
        request = ChatCompletionRequest(
            model="deepseek-v4.1-flash",
            messages=[
                {"role": "developer", "content": "dev"},
                {"role": "user", "content": "hi"},
            ],
        )

        provider._build_body(request)

        assert request.messages[0].role == "developer"


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
