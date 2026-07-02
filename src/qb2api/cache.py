"""LRU response cache with TTL for chat completions.

ponytail: dict + ordered access, no external dep. O(n) eviction is fine
for cache sizes under 1000. Upgrade to lru-dict or redis if size matters.
"""

import hashlib
import json
import logging
import threading
import time
from typing import Any

logger = logging.getLogger("qb2api")

_CACHE_KEY_FIELDS = (
    "model",
    "messages",
    "tools",
    "tool_choice",
    "stream",
    "temperature",
    "max_tokens",
    "max_completion_tokens",
    "top_p",
    "n",
)


def _make_key(request_dict: dict) -> str:
    """Build a deterministic cache key from request fields."""
    parts = {}
    for field in _CACHE_KEY_FIELDS:
        val = request_dict.get(field)
        if val is not None:
            parts[field] = val
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class ResponseCache:
    """Thread-safe LRU cache with TTL."""

    def __init__(self, max_size: int = 200, ttl: int = 300):
        self._max_size = max_size
        self._ttl = ttl
        self._store: dict[str, tuple[float, Any]] = {}  # key → (expires_at, value)
        self._lock = threading.Lock()

    def get(self, request_body: dict) -> dict | None:
        """Return cached response or None."""
        if self._max_size <= 0:
            return None
        key = _make_key(request_body)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            # Touch: move to end by re-inserting
            del self._store[key]
            self._store[key] = (expires_at, value)
            logger.debug("Cache HIT")
            return value

    def set(self, request_body: dict, response: dict) -> None:
        """Store a response in cache."""
        if self._max_size <= 0:
            return
        key = _make_key(request_body)
        with self._lock:
            # Evict oldest if at capacity
            while len(self._store) >= self._max_size:
                oldest = next(iter(self._store))
                del self._store[oldest]
            self._store[key] = (time.monotonic() + self._ttl, response)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._store)
