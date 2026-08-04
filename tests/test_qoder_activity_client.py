from __future__ import annotations

import httpx
import pytest

from qb2api.checkin import activity
from qb2api.checkin.activity import QoderActivityClient, _sign_path
from qb2api.checkin.quota import QuotaUnavailableError


@pytest.mark.asyncio
async def test_activity_client_uses_configured_path_and_normalizes_payload(monkeypatch) -> None:
    async def fake_headers(pat: str, path: str) -> dict[str, str]:
        assert pat == "pat"
        assert path == "/custom/activity"
        return {"Authorization": "Bearer redacted"}

    monkeypatch.setattr(activity, "_activity_headers", fake_headers)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": {"activities": [{
            "modelName": "Qwen", "tag": "FREE", "limit": 10,
            "used": 2, "remaining": 8, "resetAt": "tomorrow",
        }]}} , request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = QoderActivityClient(base_url="https://activity.test", path="/custom/activity", client=http)
    try:
        result = await client.fetch("pat")
    finally:
        await client.aclose()
    assert seen == ["https://activity.test/custom/activity"]
    assert result == [{
        "model": "Qwen", "tag": "FREE", "limit": 10, "used": 2,
        "remaining": 8, "reset_at": "tomorrow", "status_text": None,
        "eligible": None, "activity_end_at": None,
    }]


@pytest.mark.asyncio
async def test_activity_client_rejects_empty_activity_response(monkeypatch) -> None:
    async def fake_headers(pat: str, path: str) -> dict[str, str]:
        return _headers()

    monkeypatch.setattr(activity, "_activity_headers", fake_headers)
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"activities": []}}, request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = QoderActivityClient(client=http)
    try:
        with pytest.raises(QuotaUnavailableError, match="empty activity"):
            await client.fetch("pat")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_activity_client_accepts_list_payload_and_drops_unsafe_fields(monkeypatch) -> None:
    async def fake_headers(pat: str, path: str) -> dict[str, str]:
        return _headers()

    monkeypatch.setattr(activity, "_activity_headers", fake_headers)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{
            "model_name": "  Qwen  ", "remaining": 2,
            "tag": {"secret": "ignored"}, "metadata": {"secret": "ignored"},
        }]}, request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = QoderActivityClient(client=http)
    try:
        result = await client.fetch("pat")
    finally:
        await client.aclose()
    assert result[0]["model"] == "Qwen"
    assert result[0]["remaining"] == 2
    assert result[0]["tag"] is None


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer redacted"}


def test_sign_path_strips_algo_prefix() -> None:
    assert _sign_path("/algo/api/v2/activity") == "/api/v2/activity"
    assert _sign_path("/api/v2/activity") == "/api/v2/activity"
