"""Authenticated loopback routes used by the Proxy Worker."""

from __future__ import annotations

import hmac
import ipaddress
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from qb2api.admin.validation import json_object
from qb2api.runtime_snapshot import RUNTIME_PROTOCOL_VERSION

router = APIRouter(prefix="/api/control", tags=["internal"])


@router.post("/worker/handshake")
async def worker_handshake(request: Request) -> dict[str, Any]:
    if not _loopback(request) or not _authorized(request):
        raise HTTPException(status_code=401, detail="internal authentication required")
    body = await json_object(request)
    _validate_protocol(body)
    _validate_worker_identity(request, body)
    service = getattr(request.app.state, "runtime_snapshot_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="runtime snapshot unavailable")
    return (await service.build()).to_payload()


@router.post("/telemetry")
async def ingest_telemetry(request: Request) -> dict[str, Any]:
    if not _loopback(request) or not _authorized(request):
        raise HTTPException(status_code=401, detail="internal authentication required")
    body = await json_object(request)
    events = body.get("events")
    if not isinstance(events, list) or len(events) > 100:
        raise HTTPException(status_code=400, detail="invalid telemetry batch")
    clean = [_event(item) for item in events]
    repository = getattr(request.app.state, "account_repo", None)
    if repository is None:
        raise HTTPException(status_code=503, detail="repository unavailable")
    accepted = await repository.add_request_events(clean)
    return {"accepted": accepted}


def _authorized(request: Request) -> bool:
    expected = request.app.state.settings.worker_internal_token
    presented = request.headers.get("X-QB2API-Worker-Token", "")
    return bool(expected and hmac.compare_digest(expected, presented))


def _validate_protocol(body: dict[str, Any]) -> None:
    if body.get("protocol_version") != RUNTIME_PROTOCOL_VERSION:
        raise HTTPException(status_code=409, detail="runtime_protocol_mismatch")
    if not isinstance(body.get("owner_instance_id"), str) or not body["owner_instance_id"]:
        raise HTTPException(status_code=400, detail="worker owner is required")
    if not isinstance(body.get("internal_auth_version"), int):
        raise HTTPException(status_code=400, detail="worker auth version is required")


def _validate_worker_identity(request: Request, body: dict[str, Any]) -> None:
    supervisor = getattr(request.app.state, "supervisor", None)
    identity = supervisor.snapshot.identity if supervisor is not None else None
    if identity is None:
        raise HTTPException(status_code=409, detail="worker is not owned")
    if body["owner_instance_id"] != identity.owner_instance_id:
        raise HTTPException(status_code=409, detail="worker owner mismatch")
    if body["internal_auth_version"] != identity.internal_auth_version:
        raise HTTPException(status_code=409, detail="worker auth version mismatch")


def _loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _event(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="invalid telemetry event")
    allowed = {
        "event_id", "request_id", "provider", "account_id", "model_id", "protocol",
        "status", "http_status", "input_tokens", "output_tokens", "latency_ms",
        "stream_committed", "started_at", "finished_at", "error_code", "redacted_error",
    }
    clean = {key: value.get(key) for key in allowed if key in value}
    required = ("event_id", "request_id", "provider", "model_id", "protocol", "status")
    if any(not isinstance(clean.get(key), str) or not clean[key] for key in required):
        raise HTTPException(status_code=400, detail="telemetry event missing required fields")
    return clean
