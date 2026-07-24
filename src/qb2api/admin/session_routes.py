"""Admin login, session inspection, CSRF rotation, and logout routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .auth import (
    ADMIN_COOKIE_NAME,
    LoginRateLimiter,
    SessionCreateError,
    build_session_cookie_params,
    extract_bearer,
    verify_admin_key,
)
from .crypto import constant_time_equal
from .dependencies import admin_state, request_context, require_admin

router = APIRouter()


@router.post("/session")
async def create_session(request: Request) -> JSONResponse:
    state = admin_state(request)
    context = request_context(request)
    limiter: LoginRateLimiter = state.login_limiter
    if limiter.is_locked(context.client_ip):
        return JSONResponse(status_code=429, content={"error": "login_rate_limited"})
    presented = await _presented_key(request)
    expected = state.settings.admin_key
    if not expected or not presented or not constant_time_equal(presented, expected):
        limiter.record_failure(context.client_ip)
        return JSONResponse(status_code=401, content={"error": "invalid_admin_key"})
    limiter.record_success(context.client_ip)
    try:
        cookie = build_session_cookie_params(
            state.settings,
            is_https=context.is_https,
            is_loopback=context.is_loopback,
        )
    except SessionCreateError as error:
        return JSONResponse(status_code=400, content={"error": str(error)})
    previous = request.cookies.get(ADMIN_COOKIE_NAME)
    if previous:
        await state.admin_sessions.revoke_session(previous)
    created = await state.admin_sessions.create_session()
    return _session_response(state.settings, cookie, created)


@router.get("/session")
async def get_session(request: Request) -> dict[str, Any]:
    await require_admin(request)
    state = admin_state(request)
    cookie = request.cookies.get(ADMIN_COOKIE_NAME)
    csrf = None
    if cookie and not verify_admin_key(
        request.headers.get("Authorization"), state.settings
    ):
        csrf = await state.admin_sessions.rotate_csrf(cookie)
    return {
        "status": "ok",
        "authenticated": True,
        "csrf_token": csrf,
        "active_sessions": await state.admin_sessions.active_count(),
    }


@router.post("/session/logout")
async def logout(request: Request) -> JSONResponse:
    await require_admin(request)
    state = admin_state(request)
    cookie = request.cookies.get(ADMIN_COOKIE_NAME)
    if cookie:
        await state.admin_sessions.revoke_session(cookie)
    return _clear_cookie_response({"status": "ok"})


@router.post("/session/logout-all")
async def logout_all(request: Request) -> JSONResponse:
    await require_admin(request)
    revoked = await admin_state(request).admin_sessions.revoke_all()
    return _clear_cookie_response({"status": "ok", "revoked": revoked})


async def _presented_key(request: Request) -> str | None:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if isinstance(body, dict) and body.get("admin_key"):
        return str(body["admin_key"])
    return extract_bearer(request.headers.get("Authorization"))


def _session_response(
    settings: Any,
    cookie: dict[str, Any],
    created: dict[str, str],
) -> JSONResponse:
    response = JSONResponse(
        content={"status": "ok", "csrf_token": created["csrf_token"]}
    )
    response.set_cookie(
        cookie["key"],
        created["session_id"],
        path=cookie["path"],
        httponly=cookie["httponly"],
        samesite=cookie["samesite"],
        secure=cookie["secure"],
        max_age=settings.admin_session_ttl_hours * 3600,
    )
    return response


def _clear_cookie_response(content: dict[str, Any]) -> JSONResponse:
    response = JSONResponse(content=content)
    response.delete_cookie(ADMIN_COOKIE_NAME, path="/api/admin")
    return response
