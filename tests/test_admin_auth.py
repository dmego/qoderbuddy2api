"""Unit tests for admin auth: path matrix, keys, sessions, rate limit, cookies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from qb2api.admin.auth import (
    ADMIN_COOKIE_NAME,
    ADMIN_COOKIE_PATH,
    AdminSessionStore,
    LoginRateLimiter,
    SessionCreateError,
    build_session_cookie_params,
    classify_path,
    hash_token,
    resolve_cookie_secure,
    verify_admin_key,
    verify_proxy_key,
)
from qb2api.config import Settings

# --- classify_path ---


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/health", "public_existing"),
        ("GET", "/version", "public_existing"),
        ("GET", "/docs", "public_existing"),
        ("GET", "/openapi.json", "public_existing"),
        ("GET", "/admin", "public_admin_bootstrap"),
        ("GET", "/admin/", "public_admin_bootstrap"),
        ("GET", "/admin/accounts", "public_admin_bootstrap"),
        ("GET", "/static/admin/app.js", "public_admin_bootstrap"),
        ("POST", "/api/admin/session", "public_admin_bootstrap"),
        ("GET", "/api/admin/session", "admin_protected"),
        ("POST", "/api/admin/session/logout", "admin_protected"),
        ("GET", "/api/admin/accounts", "admin_protected"),
        ("DELETE", "/api/admin/accounts/x", "admin_protected"),
        ("GET", "/v1/models", "proxy_private"),
        ("POST", "/v1/chat/completions", "proxy_private"),
        ("POST", "/v1/messages", "proxy_private"),
        ("GET", "/api/tags", "proxy_private"),
        ("POST", "/api/show", "proxy_private"),
        ("GET", "/api/v1/models", "proxy_private"),
        ("GET", "/api/config", "admin_legacy_private"),
        ("PATCH", "/api/config", "admin_legacy_private"),
        ("GET", "/unknown", "other"),
        ("POST", "/admin", "other"),
    ],
)
def test_classify_path(method: str, path: str, expected: str):
    assert classify_path(method, path) == expected


# --- key verification ---


def test_verify_proxy_key_open_when_unset():
    settings = Settings(proxy_api_key=None, admin_ui_enabled=False)
    assert verify_proxy_key(None, settings) is True
    assert verify_proxy_key("Bearer anything", settings) is True


def test_verify_proxy_key_requires_match():
    settings = Settings(proxy_api_key="proxy-secret", admin_ui_enabled=False)
    assert verify_proxy_key("Bearer proxy-secret", settings) is True
    assert verify_proxy_key("Bearer wrong", settings) is False
    assert verify_proxy_key("proxy-secret", settings) is False
    assert verify_proxy_key(None, settings) is False
    assert verify_proxy_key("Bearer ", settings) is False


def test_verify_admin_key():
    settings = Settings(admin_key="admin-secret", admin_ui_enabled=False)
    assert verify_admin_key("Bearer admin-secret", settings) is True
    assert verify_admin_key("Bearer wrong", settings) is False
    assert verify_admin_key(None, settings) is False

    no_key = Settings(admin_key=None, admin_ui_enabled=False)
    assert verify_admin_key("Bearer x", no_key) is False


def test_proxy_key_does_not_authenticate_admin():
    settings = Settings(
        proxy_api_key="proxy-only",
        admin_key="admin-only",
        admin_ui_enabled=False,
    )
    assert verify_proxy_key("Bearer proxy-only", settings) is True
    assert verify_admin_key("Bearer proxy-only", settings) is False
    assert verify_admin_key("Bearer admin-only", settings) is True


def test_key_compare_is_constant_time_shape():
    # smoke: unequal lengths still return False without error
    settings = Settings(proxy_api_key="short", admin_ui_enabled=False)
    assert verify_proxy_key("Bearer a-much-longer-value", settings) is False


# --- cookie secure ---


def test_resolve_cookie_secure_modes():
    assert resolve_cookie_secure("true", is_https=True, is_loopback=False) is True
    with pytest.raises(SessionCreateError):
        resolve_cookie_secure("true", is_https=False, is_loopback=True)

    assert resolve_cookie_secure("auto", is_https=True, is_loopback=False) is True
    assert resolve_cookie_secure("auto", is_https=False, is_loopback=True) is False
    with pytest.raises(SessionCreateError):
        resolve_cookie_secure("auto", is_https=False, is_loopback=False)

    assert resolve_cookie_secure("false", is_https=False, is_loopback=True) is False
    assert resolve_cookie_secure("false", is_https=False, is_loopback=False) is False


def test_build_session_cookie_params():
    settings = Settings(admin_cookie_secure="auto", admin_ui_enabled=False)
    params = build_session_cookie_params(
        settings, is_https=True, is_loopback=False
    )
    assert params["key"] == ADMIN_COOKIE_NAME
    assert params["path"] == ADMIN_COOKIE_PATH
    assert params["httponly"] is True
    assert params["samesite"] == "lax"
    assert params["secure"] is True
    assert params["path"] == "/api/admin"


# --- rate limiter ---


def test_login_rate_limit_locks_after_five_failures():
    limiter = LoginRateLimiter(
        max_failures=5, window_seconds=300, lock_seconds=900
    )
    ip = "1.2.3.4"
    assert limiter.is_locked(ip) is False
    for _ in range(5):
        limiter.record_failure(ip)
    assert limiter.is_locked(ip) is True
    limiter.record_success(ip)
    assert limiter.is_locked(ip) is False


def test_login_rate_limit_is_per_ip():
    limiter = LoginRateLimiter(max_failures=2, window_seconds=300, lock_seconds=900)
    limiter.record_failure("10.0.0.1")
    limiter.record_failure("10.0.0.1")
    assert limiter.is_locked("10.0.0.1") is True
    assert limiter.is_locked("10.0.0.2") is False


def test_rate_limiter_does_not_store_keys():
    limiter = LoginRateLimiter()
    limiter.record_failure("9.9.9.9")
    # internal state keys must be IPs only
    assert all("secret" not in str(k) for k in limiter._failures)  # noqa: SLF001


# --- session store ---


@pytest.fixture
def store() -> AdminSessionStore:
    return AdminSessionStore(
        ttl_hours=12,
        idle_minutes=60,
        max_sessions=5,
    )


@pytest.mark.asyncio
async def test_create_and_validate_session(store: AdminSessionStore):
    created = await store.create_session()
    assert "session_id" in created
    assert "csrf_token" in created
    assert created["session_id"] != created["csrf_token"]
    # raw values only returned once; store holds hashes
    assert hash_token(created["session_id"]) in {
        e.session_hash for e in store._entries.values()  # noqa: SLF001
    }
    assert all(
        e.session_hash != created["session_id"] for e in store._entries.values()  # noqa: SLF001
    )

    info = await store.validate_session(created["session_id"])
    assert info is not None
    assert info.csrf_hash == hash_token(created["csrf_token"])
    assert store.verify_csrf(info, created["csrf_token"]) is True
    assert store.verify_csrf(info, "wrong") is False


@pytest.mark.asyncio
async def test_validate_rejects_unknown_and_revoked(store: AdminSessionStore):
    assert await store.validate_session("nope") is None
    created = await store.create_session()
    await store.revoke_session(created["session_id"])
    assert await store.validate_session(created["session_id"]) is None


@pytest.mark.asyncio
async def test_max_five_concurrent_sessions_revokes_oldest(store: AdminSessionStore):
    ids = []
    for _ in range(5):
        ids.append((await store.create_session())["session_id"])
    assert await store.active_count() == 5
    sixth = (await store.create_session())["session_id"]
    assert await store.active_count() == 5
    assert await store.validate_session(ids[0]) is None
    assert await store.validate_session(sixth) is not None
    for sid in ids[1:]:
        assert await store.validate_session(sid) is not None


@pytest.mark.asyncio
async def test_revoke_all(store: AdminSessionStore):
    a = (await store.create_session())["session_id"]
    b = (await store.create_session())["session_id"]
    await store.revoke_all()
    assert await store.validate_session(a) is None
    assert await store.validate_session(b) is None
    assert await store.active_count() == 0


@pytest.mark.asyncio
async def test_absolute_ttl_expiry(store: AdminSessionStore):
    store = AdminSessionStore(ttl_hours=0, idle_minutes=60, max_sessions=5)
    # ttl_hours=0 => expires immediately; use manual inject
    created = await store.create_session()
    entry = next(iter(store._entries.values()))  # noqa: SLF001
    entry.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert await store.validate_session(created["session_id"]) is None


@pytest.mark.asyncio
async def test_idle_ttl_expiry():
    store = AdminSessionStore(ttl_hours=12, idle_minutes=0, max_sessions=5)
    created = await store.create_session()
    entry = next(iter(store._entries.values()))  # noqa: SLF001
    entry.last_seen_at = datetime.now(UTC) - timedelta(minutes=1)
    # idle_minutes=0 means any positive idle fails; set last_seen far past with idle=1
    store = AdminSessionStore(ttl_hours=12, idle_minutes=1, max_sessions=5)
    created = await store.create_session()
    entry = next(iter(store._entries.values()))  # noqa: SLF001
    entry.last_seen_at = datetime.now(UTC) - timedelta(minutes=2)
    assert await store.validate_session(created["session_id"]) is None


@pytest.mark.asyncio
async def test_touch_throttled_to_one_minute():
    store = AdminSessionStore(ttl_hours=12, idle_minutes=60, max_sessions=5)
    created = await store.create_session()
    entry = next(iter(store._entries.values()))  # noqa: SLF001
    original = entry.last_seen_at
    # second validate immediately should not move last_seen beyond same second window
    await store.validate_session(created["session_id"], touch=True)
    assert entry.last_seen_at == original
    entry.last_seen_at = original - timedelta(seconds=61)
    await store.validate_session(created["session_id"], touch=True)
    assert entry.last_seen_at > original - timedelta(seconds=61)


def test_hash_token_stable_and_not_plaintext():
    raw = "super-secret-session-id"
    h = hash_token(raw)
    assert h == hash_token(raw)
    assert h != raw
    assert len(h) == 64
