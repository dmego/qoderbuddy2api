"""In-memory OAuth flow store.

AUTH-01 related: TTL 15min, one-time consume, label + hashed state.
Raw state stays process-local only; public views never include it.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

DEFAULT_FLOW_TTL_SECONDS = 15 * 60  # 15 min


def hash_state(auth_state: str) -> str:
    return hashlib.sha256(auth_state.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class FlowRecord:
    """Public flow view — no raw state or tokens."""

    flow_id: str
    label: str
    state_hash: str
    auth_url: str
    expires_at: float


@dataclass(slots=True)
class _FlowEntry:
    record: FlowRecord
    auth_state: str  # process-local only
    consumed: bool = False


@dataclass(frozen=True, slots=True)
class FlowPollLease:
    record: FlowRecord
    auth_state: str


class FlowBusyError(RuntimeError):
    """Another request is already polling this OAuth flow."""


class FlowStore:
    """TTL + one-time consume store for admin OAuth flows."""

    def __init__(self, ttl_seconds: int = DEFAULT_FLOW_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._flows: dict[str, _FlowEntry] = {}
        self._polling: set[str] = set()

    def create(self, *, label: str, auth_state: str, auth_url: str) -> FlowRecord:
        self._purge_expired()
        flow_id = secrets.token_urlsafe(16)
        expires_at = time.time() + self._ttl
        record = FlowRecord(
            flow_id=flow_id,
            label=label,
            state_hash=hash_state(auth_state),
            auth_url=auth_url,
            expires_at=expires_at,
        )
        self._flows[flow_id] = _FlowEntry(record=record, auth_state=auth_state)
        return record

    def get(self, flow_id: str) -> FlowRecord | None:
        entry = self._alive(flow_id)
        return entry.record if entry else None

    def get_state(self, flow_id: str) -> str | None:
        """Return raw state for upstream poll (not for UI)."""
        entry = self._alive(flow_id)
        return entry.auth_state if entry else None

    def consume(self, flow_id: str) -> bool:
        """One-time consume after successful login. Returns False if gone/used/expired."""
        entry = self._alive(flow_id)
        if entry is None:
            return False
        entry.consumed = True
        del self._flows[flow_id]
        return True

    def begin_poll(self, flow_id: str) -> FlowPollLease:
        entry = self._alive(flow_id)
        if entry is None:
            raise LookupError("flow_not_found_or_expired")
        if flow_id in self._polling:
            raise FlowBusyError("flow_poll_in_progress")
        self._polling.add(flow_id)
        return FlowPollLease(record=entry.record, auth_state=entry.auth_state)

    def finish_poll(self, flow_id: str, *, consume: bool) -> None:
        self._polling.discard(flow_id)
        if consume:
            self.consume(flow_id)

    def _alive(self, flow_id: str) -> _FlowEntry | None:
        entry = self._flows.get(flow_id)
        if entry is None:
            return None
        if entry.consumed or entry.record.expires_at <= time.time():
            self._flows.pop(flow_id, None)
            self._polling.discard(flow_id)
            return None
        return entry

    def _purge_expired(self) -> None:
        now = time.time()
        dead = [
            fid
            for fid, e in self._flows.items()
            if e.consumed or e.record.expires_at <= now
        ]
        for fid in dead:
            self._flows.pop(fid, None)
            self._polling.discard(fid)
