"""Admin auth policy: path matrix, key checks, HttpOnly session/CSRF.

Design §7.1 / §7.2. Never log or return Admin/Proxy keys or raw session secrets
after create. DB/process store only hashes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from qb2api.config import Settings

from .crypto import constant_time_equal as _consteq
from .crypto import hash_token
from .sessions import AdminSessionStore, SessionInfo

__all__ = ["AdminSessionStore", "SessionInfo", "hash_token"]

PathClass = Literal[
    "public_existing",
    "public_admin_bootstrap",
    "admin_protected",
    "proxy_private",
    "admin_legacy_private",
    "other",
]

ADMIN_COOKIE_NAME = "qb2api_admin_session"
ADMIN_COOKIE_PATH = "/api/admin"

_PUBLIC_EXISTING = frozenset({"/health", "/version", "/docs", "/openapi.json", "/redoc"})
_PROXY_EXACT = frozenset({"/api/tags", "/api/show"})


class SessionCreateError(ValueError):
    """Cookie Secure policy rejected the request context."""


def _normalize_path(path: str) -> str:
    path = path.split("?", 1)[0] or "/"
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path


def classify_path(method: str, path: str) -> PathClass:
    """Classify method+path for auth middleware (design §7.1)."""
    method = (method or "GET").upper()
    path = _normalize_path(path)

    if method == "GET" and path in _PUBLIC_EXISTING:
        return "public_existing"

    if method == "POST" and path == "/api/admin/session":
        return "public_admin_bootstrap"

    if method == "GET" and (
        path == "/admin"
        or path.startswith("/admin/")
        or path.startswith("/admin/assets/")
        or path.startswith("/static/admin/")
    ):
        return "public_admin_bootstrap"

    if path == "/api/admin" or path.startswith("/api/admin/"):
        return "admin_protected"

    if path == "/api/config" or path.startswith("/api/config/"):
        return "admin_legacy_private"

    if path.startswith("/v1") or path in _PROXY_EXACT or path.startswith("/api/v1/"):
        return "proxy_private"

    return "other"


def extract_bearer(auth_header: str | None) -> str | None:
    if not auth_header:
        return None
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header[7:]
    return token if token else None


def verify_proxy_key(auth_header: str | None, settings: Settings) -> bool:
    """True when proxy key unset (open proxy) or Bearer matches proxy key."""
    expected = settings.proxy_api_key
    if not expected:
        return True
    token = extract_bearer(auth_header)
    if token is None:
        return False
    return _consteq(token, expected)


def verify_admin_key(auth_header: str | None, settings: Settings) -> bool:
    """True only when Bearer matches configured admin key."""
    expected = settings.admin_key
    if not expected:
        return False
    token = extract_bearer(auth_header)
    if token is None:
        return False
    return _consteq(token, expected)


def resolve_cookie_secure(
    mode: str, *, is_https: bool, is_loopback: bool
) -> bool:
    """Map QB2API_ADMIN_COOKIE_SECURE to Secure flag (design §7.2)."""
    mode = (mode or "auto").strip().lower()
    if mode == "true":
        if not is_https:
            raise SessionCreateError("admin cookie secure=true requires HTTPS")
        return True
    if mode == "false":
        # This is an explicit operator override for trusted LAN/Tailscale HTTP.
        # Keep ``auto`` secure by default; ``false`` deliberately accepts the
        # transport risk and only controls the browser Cookie Secure flag.
        return False
    # auto
    if is_https:
        return True
    if is_loopback:
        return False
    raise SessionCreateError(
        "admin session requires HTTPS for non-loopback (cookie secure=auto)"
    )


def build_session_cookie_params(
    settings: Settings, *, is_https: bool, is_loopback: bool
) -> dict[str, Any]:
    secure = resolve_cookie_secure(
        settings.admin_cookie_secure, is_https=is_https, is_loopback=is_loopback
    )
    return {
        "key": ADMIN_COOKIE_NAME,
        "path": ADMIN_COOKIE_PATH,
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
    }


@dataclass
class LoginRateLimiter:
    """In-memory login failure limiter: 5 / 5min then lock 15min per IP."""

    max_failures: int = 5
    window_seconds: int = 300
    lock_seconds: int = 900
    _failures: dict[str, list[float]] = field(default_factory=dict)
    _locked_until: dict[str, float] = field(default_factory=dict)

    def is_locked(self, ip: str) -> bool:
        import time

        now = time.monotonic()
        until = self._locked_until.get(ip)
        if until is not None and until > now:
            return True
        if until is not None:
            self._locked_until.pop(ip, None)
        return False

    def record_failure(self, ip: str) -> None:
        import time

        now = time.monotonic()
        window_start = now - self.window_seconds
        stamps = [t for t in self._failures.get(ip, []) if t >= window_start]
        stamps.append(now)
        self._failures[ip] = stamps
        if len(stamps) >= self.max_failures:
            self._locked_until[ip] = now + self.lock_seconds
            self._failures[ip] = []

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)
        self._locked_until.pop(ip, None)
