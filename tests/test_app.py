"""Tests for qoderbuddy2api: model resolution, SSE aggregation, Qoder tool calls, error handling, logging."""

import json
import pytest
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Model resolution tests
# ---------------------------------------------------------------------------
class TestModelResolution:
    """Test _resolve_model() routing logic."""

    def _setup_registry(self, providers: dict[str, list[str]]):
        """Set up app state with given providers and model lists."""
        from qb2api.app import _model_index, registry
        from qb2api.models import ModelDefinition

        registry.clear()
        _model_index.clear()

        model_defs = {}
        for provider_name, model_ids in providers.items():
            provider = MagicMock()
            provider.name = provider_name
            registry.register(provider)
            _model_index[provider_name] = set(model_ids)
            model_defs[provider_name] = [ModelDefinition(mid, mid, provider_name) for mid in model_ids]

        return model_defs

    def test_explicit_provider_routing(self):
        from qb2api.app import _resolve_model

        self._setup_registry({"codebuddy": ["deepseek-v3"], "qoder": ["auto"]})

        provider, model = _resolve_model("codebuddy/deepseek-v3")
        assert provider == "codebuddy"
        assert model == "deepseek-v3"

    def test_explicit_provider_unknown_model(self):
        from qb2api.app import _resolve_model
        from fastapi import HTTPException

        self._setup_registry({"codebuddy": ["deepseek-v3"]})

        with pytest.raises(HTTPException) as exc_info:
            _resolve_model("codebuddy/nonexistent")
        assert exc_info.value.status_code == 400

    def test_explicit_provider_unknown_provider(self):
        from qb2api.app import _resolve_model
        from fastapi import HTTPException

        self._setup_registry({"codebuddy": ["deepseek-v3"]})

        with pytest.raises(HTTPException) as exc_info:
            _resolve_model("qoder/auto")
        assert exc_info.value.status_code == 400

    def test_bare_model_single_match(self):
        from qb2api.app import _resolve_model

        self._setup_registry({"codebuddy": ["deepseek-v3"]})

        provider, model = _resolve_model("deepseek-v3")
        assert provider == "codebuddy"
        assert model == "deepseek-v3"

    def test_bare_model_ambiguous(self):
        from qb2api.app import _resolve_model
        from fastapi import HTTPException

        self._setup_registry({"codebuddy": ["auto"], "qoder": ["auto"]})

        with pytest.raises(HTTPException) as exc_info:
            _resolve_model("auto")
        assert exc_info.value.status_code == 400
        assert "Ambiguous" in str(exc_info.value.detail)

    def test_bare_model_unknown(self):
        from qb2api.app import _resolve_model
        from fastapi import HTTPException

        self._setup_registry({"codebuddy": ["deepseek-v3"]})

        with pytest.raises(HTTPException) as exc_info:
            _resolve_model("totally-unknown")
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# SSE aggregation tests
# ---------------------------------------------------------------------------
class TestStreamAggregator:
    """Test StreamAggregator behavior."""

    def test_aggregates_content(self):
        from qb2api.sse import StreamAggregator

        agg = StreamAggregator(model="test")
        agg.process({"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]})
        agg.process({"choices": [{"delta": {"content": " World"}, "finish_reason": "stop"}]})

        resp = agg.response()
        assert resp["choices"][0]["message"]["content"] == "Hello World"
        assert resp["choices"][0]["finish_reason"] == "stop"

    def test_aggregates_reasoning(self):
        from qb2api.sse import StreamAggregator

        agg = StreamAggregator(model="test")
        agg.process({"choices": [{"delta": {"reasoning_content": "Think"}, "finish_reason": None}]})
        agg.process({"choices": [{"delta": {"content": "Answer"}, "finish_reason": "stop"}]})

        resp = agg.response()
        assert resp["choices"][0]["message"]["reasoning_content"] == "Think"
        assert resp["choices"][0]["message"]["content"] == "Answer"

    def test_aggregates_multiple_tool_calls_with_index(self):
        from qb2api.sse import StreamAggregator

        agg = StreamAggregator(model="test")
        # First tool call
        agg.process({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_aaa", "function": {"name": "get_weather", "arguments": ""}}
        ]}, "finish_reason": None}]})
        agg.process({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": '{"city":"Tokyo"}'}}
        ]}, "finish_reason": None}]})
        # Second tool call
        agg.process({"choices": [{"delta": {"tool_calls": [
            {"index": 1, "id": "call_bbb", "function": {"name": "get_time", "arguments": ""}}
        ]}, "finish_reason": None}]})
        agg.process({"choices": [{"delta": {"tool_calls": [
            {"index": 1, "function": {"arguments": '{"tz":"JST"}'}}
        ]}, "finish_reason": "tool_calls"}]})

        resp = agg.response()
        assert resp["choices"][0]["finish_reason"] == "tool_calls"
        tcs = resp["choices"][0]["message"]["tool_calls"]
        assert len(tcs) == 2
        assert tcs[0]["function"]["name"] == "get_weather"
        assert tcs[0]["function"]["arguments"] == '{"city":"Tokyo"}'
        assert tcs[1]["function"]["name"] == "get_time"
        assert tcs[1]["function"]["arguments"] == '{"tz":"JST"}'

    def test_created_is_not_zero(self):
        from qb2api.sse import StreamAggregator

        agg = StreamAggregator(model="test")
        agg.process({"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]})
        resp = agg.response()
        assert resp["created"] > 0


# ---------------------------------------------------------------------------
# CodeBuddy system scrub tests
# ---------------------------------------------------------------------------
class TestCodeBuddyScrub:
    """CodeBuddy rejects Claude Code identity phrasing; scrub on outbound only."""

    def test_scrub_replaces_claude_code_identity(self):
        from qb2api.providers.codebuddy import scrub_codebuddy_text

        src = "You are Claude Code, Anthropic's official CLI for Claude.\nHelp with code."
        out = scrub_codebuddy_text(src)
        assert "Claude Code" not in out
        assert "Anthropic" not in out
        assert out.startswith("You are a coding CLI assistant.")
        assert "Help with code." in out

    def test_scrub_leaves_normal_system_untouched(self):
        from qb2api.providers.codebuddy import scrub_codebuddy_text

        src = "You are a helpful coding assistant."
        assert scrub_codebuddy_text(src) == src

    def test_build_body_scrubs_system_only(self):
        from qb2api.openai import ChatCompletionRequest
        from qb2api.providers.codebuddy import CodeBuddyProvider

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
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        assert system.startswith("You are a coding CLI assistant.")
        assert "Anthropic" not in system
        # user content is not rewritten
        assert "Claude Code" in user


# ---------------------------------------------------------------------------
# Qoder tool call parsing tests
# ---------------------------------------------------------------------------
class TestQoderToolCalls:
    """Test Qoder tool call parsing (CLI-based parsing no longer needed with COSY)."""

    def test_qoder_maps_cli_display_model_to_internal_key(self):
        """Qoder CLI named models use internal model keys in COSY requests."""
        from qb2api.providers.qoder import QODER_CLI_MODEL_KEYS

        assert QODER_CLI_MODEL_KEYS["Qwen3.8-Max-Preview"] == "qmodel_preview"
        assert QODER_CLI_MODEL_KEYS["Qwen3.7-Max"] == "qmodel_latest"
        assert QODER_CLI_MODEL_KEYS["Kimi-K2.7-Code"] == "kmodel"
        assert QODER_CLI_MODEL_KEYS["DeepSeek-V4-Pro"] == "dmodel"

    def test_qoder_payload_uses_internal_key_for_named_model(self):
        """Named Qoder models must not be sent upstream as auto."""
        from qb2api.openai import ChatCompletionRequest
        from qb2api.providers.qoder import QoderProvider

        provider = QoderProvider(pat="dummy")
        request = ChatCompletionRequest(
            model="Qwen3.7-Max",
            messages=[{"role": "user", "content": "hi"}],
        )

        payload = provider._build_payload(request, "Qwen3.7-Max")

        assert payload["model_config"] == {"key": "qmodel_latest", "source": "system"}

    def test_cosy_provider_has_no_cli_parser(self):
        """COSY provider doesn't use _parse_tool_calls (that was CLI-only)."""
        from qb2api.providers.qoder import QoderProvider

        provider = QoderProvider(pat="dummy")
        assert not hasattr(provider, '_parse_tool_calls')

    def test_qoder_session_builds_headers(self):
        """QoderSession.chat_headers returns COSY headers."""
        from qb2api.providers.qoder import QoderSession

        session = QoderSession(pat="dummy")
        session.user_id = "test-user"
        session.cosy_key = "test-key"
        session.payload_b64 = "test-payload"
        session._ready = True

        headers = session.chat_headers("test-body", "auto")
        assert "authorization" in headers
        assert headers["authorization"].startswith("Bearer COSY.")
        assert headers["cosy-version"] == "0.1.43"


# ---------------------------------------------------------------------------
# Logger tests
# ---------------------------------------------------------------------------
class TestLogger:
    """Test RequestLogger doesn't crash on various inputs."""

    def test_tool_calls_count_does_not_crash(self, tmp_path):
        from qb2api.logger import RequestLogger

        logger = RequestLogger(log_dir=str(tmp_path), enabled=True)
        # This used to crash with TypeError: object of type 'int' has no len()
        logger.log_request(
            model="deepseek-v3", provider="codebuddy", stream=False,
            success=True, duration=1.0, tool_calls_count=2,
        )

    def test_logger_never_raises(self, tmp_path):
        from qb2api.logger import RequestLogger

        logger = RequestLogger(log_dir=str(tmp_path), enabled=True)
        # Even with completely invalid kwargs, logger should not raise
        logger.log_request(
            model="test", provider="test", stream=False,
            success=True, duration=0.1, some_random_kwarg=object(),
        )

    def test_error_logged_with_status_code(self, tmp_path):
        from qb2api.logger import RequestLogger

        logger = RequestLogger(log_dir=str(tmp_path), enabled=True)
        logger.log_request(
            model="deepseek-v3", provider="codebuddy", stream=False,
            success=False, duration=0.5, status_code=401, error="Unauthorized",
        )

        # Read the log file
        log_files = list(tmp_path.glob("*.jsonl"))
        assert len(log_files) == 1
        entry = json.loads(log_files[0].read_text().strip())
        assert entry["success"] is False
        assert entry["status_code"] == 401
        assert entry["error"] == "Unauthorized"


# ---------------------------------------------------------------------------
# JSON error handling tests
# ---------------------------------------------------------------------------
class TestJsonErrorHandling:
    """Test that invalid JSON returns proper 400."""

    def test_invalid_json_returns_structured_error(self):
        from fastapi.testclient import TestClient
        from qb2api.app import app

        # Need to set up minimal state
        with patch("qb2api.app.registry") as mock_registry:
            mock_registry.providers = ["codebuddy"]
            client = TestClient(app)
            resp = client.post(
                "/v1/chat/completions",
                content=b"{bad json",
                headers={"Content-Type": "application/json"},
            )
            # Should be 400 with structured error, not 500
            assert resp.status_code == 400
            body = resp.json()
            assert "error" in body


# ---------------------------------------------------------------------------
# API auth tests
# ---------------------------------------------------------------------------
class TestApiAuth:
    """Test optional local API key enforcement."""

    def test_configured_api_key_blocks_private_endpoints_without_bearer(self):
        from fastapi.testclient import TestClient
        from qb2api.app import app
        from qb2api.config import Settings

        with patch("qb2api.app.settings", Settings(api_key="secret")):
            client = TestClient(app)
            resp = client.get("/v1/models")

        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_configured_api_key_allows_matching_bearer(self):
        from fastapi.testclient import TestClient
        from qb2api.app import app, registry
        from qb2api.config import Settings

        registry.clear()
        with patch("qb2api.app.settings", Settings(api_key="secret")):
            client = TestClient(app)
            resp = client.get("/v1/models", headers={"Authorization": "Bearer secret"})

        assert resp.status_code == 200

    def test_health_is_public_even_when_api_key_is_configured(self):
        from fastapi.testclient import TestClient
        from qb2api.app import app
        from qb2api.config import Settings

        with patch("qb2api.app.settings", Settings(api_key="secret")):
            client = TestClient(app)
            resp = client.get("/health")

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /v1/models filtering tests
# ---------------------------------------------------------------------------
class TestModelsFiltering:
    """Test that /v1/models only returns models from registered providers."""

    def test_empty_providers_returns_empty_models(self):
        from qb2api.app import _available_models, registry

        registry.clear()
        result = _available_models()
        assert result == {}
