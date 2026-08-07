"""Admin request authentication and trusted network context."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from .auth import ADMIN_COOKIE_NAME, AdminSessionStore, verify_admin_key

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, slots=True)
class AdminRequestContext:
    client_ip: str
    is_https: bool
    is_loopback: bool


def admin_state(request: Request) -> Any:
    return request.app.state


def request_context(request: Request) -> AdminRequestContext:
    peer = request.client.host if request.client and request.client.host else "unknown"
    trusted = _trusted_proxy_peer(request, peer)
    client_ip = _forwarded_client(request, peer) if trusted else peer
    is_https = request.url.scheme == "https" or (
        trusted and _forwarded_proto(request) == "https"
    )
    return AdminRequestContext(
        client_ip=client_ip,
        is_https=is_https,
        is_loopback=_is_loopback(client_ip),
    )


async def require_admin(request: Request) -> dict[str, Any]:
    state = admin_state(request)
    if verify_admin_key(request.headers.get("Authorization"), state.settings):
        return {"auth": "bearer"}
    store: AdminSessionStore = state.admin_sessions
    cookie = request.cookies.get(ADMIN_COOKIE_NAME)
    info = await store.validate_session(cookie)
    if info is None:
        raise HTTPException(status_code=401, detail="admin authentication required")
    if request.method.upper() in _MUTATING:
        csrf = request.headers.get("X-CSRF-Token")
        if not store.verify_csrf(info, csrf):
            raise HTTPException(status_code=403, detail="csrf_failed")
    return {"auth": "session", "session": info}


def _trusted_proxy_peer(request: Request, peer: str) -> bool:
    settings = admin_state(request).settings
    networks = settings.trusted_proxy_networks or []
    if not settings.trusted_proxy_headers or not networks:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(_belongs_to(address, value) for value in networks)


def _belongs_to(address: ipaddress.IPv4Address | ipaddress.IPv6Address, value: str) -> bool:
    try:
        return address in ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False


def _forwarded_client(request: Request, fallback: str) -> str:
    first = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not first:
        return fallback
    try:
        return str(ipaddress.ip_address(first))
    except ValueError:
        return fallback


def _forwarded_proto(request: Request) -> str:
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
