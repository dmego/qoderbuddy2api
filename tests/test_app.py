"""Worker model resolution and HTTP application tests."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from qb2api.config import Settings
from qb2api.models import ModelDefinition
from qb2api.worker.app import create_worker_app
from qb2api.worker.proxy_state import ProxyState


class TestModelResolution:
    """Test Worker model routing logic."""

    def _setup_state(self, providers: dict[str, list[str]]) -> ProxyState:
        state = ProxyState(Settings())
        model_defs = {}
        for provider_name, model_ids in providers.items():
            provider = MagicMock()
            provider.name = provider_name
            state.registry.register(provider)
            model_defs[provider_name] = [
                ModelDefinition(model_id, model_id, provider_name)
                for model_id in model_ids
            ]
        state.model_definitions = model_defs
        state._build_model_index()
        return state

    def test_explicit_provider_routing(self):
        state = self._setup_state({"codebuddy": ["deepseek-v3"], "qoder": ["auto"]})

        provider, model = state.resolve_model("codebuddy/deepseek-v3")

        assert provider == "codebuddy"
        assert model == "deepseek-v3"

    def test_explicit_provider_unknown_model(self):
        from fastapi import HTTPException

        state = self._setup_state({"codebuddy": ["deepseek-v3"]})

        with pytest.raises(HTTPException) as exc_info:
            state.resolve_model("codebuddy/nonexistent")
        assert exc_info.value.status_code == 400

    def test_explicit_provider_unknown_provider(self):
        from fastapi import HTTPException

        state = self._setup_state({"codebuddy": ["deepseek-v3"]})

        with pytest.raises(HTTPException) as exc_info:
            state.resolve_model("qoder/auto")
        assert exc_info.value.status_code == 400

    def test_bare_model_single_match(self):
        state = self._setup_state({"codebuddy": ["deepseek-v3"]})

        provider, model = state.resolve_model("deepseek-v3")

        assert provider == "codebuddy"
        assert model == "deepseek-v3"

    def test_bare_model_ambiguous(self):
        from fastapi import HTTPException

        state = self._setup_state({"codebuddy": ["auto"], "qoder": ["auto"]})

        with pytest.raises(HTTPException) as exc_info:
            state.resolve_model("auto")
        assert exc_info.value.status_code == 400
        assert "Ambiguous" in str(exc_info.value.detail)

    def test_bare_model_unknown(self):
        from fastapi import HTTPException

        state = self._setup_state({"codebuddy": ["deepseek-v3"]})

        with pytest.raises(HTTPException) as exc_info:
            state.resolve_model("totally-unknown")
        assert exc_info.value.status_code == 400


class TestJsonErrorHandling:
    """Test that invalid JSON returns proper 400."""

    def test_invalid_json_returns_structured_error(self):
        application = create_worker_app(lambda: Settings(codebuddy_tokens=["ck-worker"]))
        with TestClient(application) as client:
            response = client.post(
                "/v1/chat/completions",
                content=b"{bad json",
                headers={"Content-Type": "application/json"},
            )

        assert response.status_code == 400
        assert "error" in response.json()


class TestApiAuth:
    """Test optional local API key enforcement."""

    def test_configured_api_key_blocks_private_endpoints_without_bearer(self):
        application = create_worker_app(
            lambda: Settings(codebuddy_tokens=["ck-worker"], proxy_api_key="secret")
        )
        with TestClient(application) as client:
            response = client.get("/v1/models")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"

    def test_configured_api_key_allows_matching_bearer(self):
        application = create_worker_app(
            lambda: Settings(codebuddy_tokens=["ck-worker"], proxy_api_key="secret")
        )
        with TestClient(application) as client:
            response = client.get("/v1/models", headers={"Authorization": "Bearer secret"})

        assert response.status_code == 200

    def test_health_is_public_even_when_api_key_is_configured(self):
        application = create_worker_app(
            lambda: Settings(codebuddy_tokens=["ck-worker"], proxy_api_key="secret")
        )
        with TestClient(application) as client:
            response = client.get("/health")

        assert response.status_code == 200


class TestModelsFiltering:
    """Test that /v1/models only returns models from registered providers."""

    def test_empty_providers_returns_empty_models(self):
        assert ProxyState(Settings()).available_models() == {}
