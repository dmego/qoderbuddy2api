"""Control Plane unified-port forwarding of /v1 proxy traffic."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from qb2api.config import Settings
from qb2api.control.app import create_control_app


def _app_with_mock_worker(handler):
    settings = Settings(
        admin_key="admin-secret",
        worker_host="127.0.0.1",
        worker_port=10001,
        admin_ui_enabled=False,
    )
    application = create_control_app(lambda: settings)
    application.state.proxy_forward_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    return application


def test_v1_models_forwarded_to_worker():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={"models": [{"id": "deepseek-v4-flash"}]},
            headers={"content-type": "application/json"},
        )

    app = _app_with_mock_worker(handler)
    with TestClient(app) as client:
        response = client.get(
            "/v1/models", headers={"Authorization": "Bearer proxy-secret"}
        )

    assert response.status_code == 200
    assert response.json()["models"][0]["id"] == "deepseek-v4-flash"
    assert seen["url"] == "http://127.0.0.1:10001/v1/models"
    assert seen["auth"] == "Bearer proxy-secret"


def test_v1_messages_body_and_stream_forwarded():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            text='data: {"type":"content_block_delta"}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"},
        )

    app = _app_with_mock_worker(handler)
    payload = '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hi"}]}'
    with TestClient(app) as client:
        response = client.post(
            "/v1/messages",
            content=payload,
            headers={"Authorization": "Bearer proxy-secret", "Content-Type": "application/json"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream"
    assert 'data: {"type":"content_block_delta"}' in response.text
    assert seen["method"] == "POST"
    assert seen["url"] == "http://127.0.0.1:10001/v1/messages"
    assert '"model":"deepseek-v4-flash"' in seen["body"]


def test_non_v1_paths_are_not_forwarded():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, text="unexpected")

    app = _app_with_mock_worker(handler)
    with TestClient(app) as client:
        response = client.get("/api/admin/health", headers={"Authorization": "Bearer admin-secret"})

    assert called is False
    assert response.status_code == 404
