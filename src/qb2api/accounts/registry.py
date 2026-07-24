"""Merge env static slots with DB dynamic accounts; publish purpose snapshots."""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from typing import Any

from .models import AccountSlot, AccountView
from .repository import AccountRepository
from .vault import CredentialVault

# purpose 可用状态：进入 chat pool 的候选
_CHAT_OK_STATUS = frozenset({"active"})


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "***"
    return f"{secret[:3]}***{secret[-2:]}"


def _secrets_equal(a: str, b: str) -> bool:
    """Constant-time equality for secret strings of equal length."""
    if a is None or b is None:
        return False
    ba, bb = a.encode(), b.encode()
    if len(ba) != len(bb):
        # length mismatch: still do a dummy compare to reduce timing branch noise
        return hmac.compare_digest(ba, ba) and False
    return hmac.compare_digest(ba, bb)


def _summary_status(
    *,
    enabled: bool,
    purposes: dict[str, dict[str, Any]],
) -> str:
    if not enabled:
        return "disabled"
    action = {"needs_reauth", "expired", "invalid", "needs_import"}
    for p in purposes.values():
        if p.get("enabled") and p.get("status") in action:
            return "action_required"
    for p in purposes.values():
        if p.get("enabled") and p.get("status") == "active":
            return "active"
    return "pending"


@dataclass
class _EnvSlot:
    provider: str
    account_id: str
    secret: str
    index: int
    shadowed: bool = False


@dataclass
class _DynSlot:
    provider: str
    account_id: str
    label: str
    source: str
    enabled: bool
    masked_identity: str | None
    created_at: str | None
    updated_at: str | None
    purposes: dict[str, dict[str, Any]] = field(default_factory=dict)
    # purpose -> plaintext primary secret (chat only, for shadow compare)
    chat_secret: str | None = None


class AccountRegistry:
    """In-memory merge of env static slots + DB dynamic accounts.

    Env secrets stay in process memory only — never written to SQLite.
    """

    def __init__(
        self,
        repo: AccountRepository,
        vault: CredentialVault | None = None,
        *,
        codebuddy_tokens: list[str] | None = None,
        qoder_tokens: list[str] | None = None,
    ) -> None:
        self._repo = repo
        self._vault = vault
        self._codebuddy_tokens = list(codebuddy_tokens or [])
        self._qoder_tokens = list(qoder_tokens or [])
        self._env: list[_EnvSlot] = []
        self._dyn: dict[tuple[str, str], _DynSlot] = {}
        # (provider, account_id, purpose) -> secret for env-only resolve
        self._env_secrets: dict[tuple[str, str, str], str] = {}
        self._built = False

    def set_env_tokens(
        self,
        *,
        codebuddy_tokens: list[str] | None = None,
        qoder_tokens: list[str] | None = None,
    ) -> None:
        if codebuddy_tokens is not None:
            self._codebuddy_tokens = list(codebuddy_tokens)
        if qoder_tokens is not None:
            self._qoder_tokens = list(qoder_tokens)

    async def rebuild(self) -> None:
        """Reload env + DB into memory snapshots."""
        env: list[_EnvSlot] = []
        env_secrets: dict[tuple[str, str, str], str] = {}
        for i, tok in enumerate(self._codebuddy_tokens):
            if not tok:
                continue
            aid = f"cb-env-{i}"
            env.append(_EnvSlot("codebuddy", aid, tok, i))
            env_secrets[("codebuddy", aid, "chat")] = tok
        for i, tok in enumerate(self._qoder_tokens):
            if not tok:
                continue
            aid = f"qd-env-{i}"
            env.append(_EnvSlot("qoder", aid, tok, i))
            env_secrets[("qoder", aid, "chat")] = tok

        dyn: dict[tuple[str, str], _DynSlot] = {}
        accounts = await self._repo.list_accounts()
        for acc in accounts:
            key = (acc["provider"], acc["account_id"])
            purposes_list = await self._repo.list_purposes(acc["provider"], acc["account_id"])
            purpose_map: dict[str, dict[str, Any]] = {}
            chat_secret: str | None = None
            for p in purposes_list:
                purpose_map[p["purpose"]] = {
                    "enabled": p["enabled"],
                    "status": p["status"],
                    "verification_status": p["verification_status"],
                    "capabilities": list(p.get("capabilities") or []),
                    "verified_at": p.get("verified_at"),
                    "expires_at": p.get("expires_at"),
                    "last_success_at": p.get("last_success_at"),
                    "failure_count": p.get("failure_count", 0),
                    "last_error": p.get("last_error"),
                }
                if (
                    p["purpose"] == "chat"
                    and self._vault is not None
                    and acc.get("enabled")
                    and p.get("enabled")
                ):
                    cred = await self._repo.get_credential(
                        acc["provider"], acc["account_id"], "chat"
                    )
                    if cred:
                        try:
                            payload = self._vault.decrypt(cred["encrypted_payload"])
                            chat_secret = self._primary_secret(acc["provider"], payload)
                        except ValueError:
                            chat_secret = None
            dyn[key] = _DynSlot(
                provider=acc["provider"],
                account_id=acc["account_id"],
                label=acc.get("label") or acc["account_id"],
                source=acc.get("source") or "manual",
                enabled=bool(acc.get("enabled")),
                masked_identity=acc.get("masked_identity"),
                created_at=acc.get("created_at"),
                updated_at=acc.get("updated_at"),
                purposes=purpose_map,
                chat_secret=chat_secret,
            )

        # shadow: dynamic secret wins over same env secret
        for e in env:
            for d in dyn.values():
                if d.provider != e.provider or not d.enabled:
                    continue
                if d.chat_secret and _secrets_equal(d.chat_secret, e.secret):
                    e.shadowed = True
                    break

        self._env = env
        self._dyn = dyn
        self._env_secrets = env_secrets
        self._built = True

    @staticmethod
    def _primary_secret(provider: str, payload: dict[str, Any]) -> str | None:
        if provider == "qoder":
            return payload.get("pat") or payload.get("access_token")
        return payload.get("access_token") or payload.get("token")

    def env_secret(self, provider: str, account_id: str, purpose: str = "chat") -> str | None:
        """Return env plaintext secret if present and not shadowed (internal only)."""
        for e in self._env:
            if e.provider == provider and e.account_id == account_id:
                if e.shadowed:
                    return None
                return self._env_secrets.get((provider, account_id, purpose))
        return None

    def is_env_account(self, provider: str, account_id: str) -> bool:
        return any(e.provider == provider and e.account_id == account_id for e in self._env)

    def snapshot(self, purpose: str = "chat") -> list[AccountSlot]:
        """Active, non-shadowed slots for a purpose (pool membership)."""
        self._ensure_built()
        slots: list[AccountSlot] = []
        # env static tokens: chat only (design: ck_/pt_ default not for check-in)
        if purpose == "chat":
            for e in self._env:
                if e.shadowed:
                    continue
                slots.append(
                    AccountSlot(
                        provider=e.provider,
                        account_id=e.account_id,
                        source="env",
                        enabled=True,
                        status="active",
                        verification_status="not_required",
                        shadowed=False,
                        capabilities=["proxy.chat"],
                    )
                )
        for d in sorted(self._dyn.values(), key=lambda x: (x.provider, x.account_id)):
            if not d.enabled:
                continue
            p = d.purposes.get(purpose)
            if not p or not p.get("enabled"):
                continue
            if p.get("status") not in _CHAT_OK_STATUS and purpose == "chat":
                continue
            if purpose == "checkin":
                if p.get("status") != "active" or p.get("verification_status") != "verified":
                    continue
            slots.append(
                AccountSlot(
                    provider=d.provider,
                    account_id=d.account_id,
                    source=d.source,
                    enabled=True,
                    status=p["status"],
                    verification_status=p["verification_status"],
                    shadowed=False,
                    capabilities=list(p.get("capabilities") or []),
                )
            )
        return slots

    def list_views(self) -> list[AccountView]:
        """UI-safe account views (no secrets)."""
        self._ensure_built()
        views: list[AccountView] = []
        for e in self._env:
            purposes = {
                "chat": {
                    "enabled": not e.shadowed,
                    "status": "active" if not e.shadowed else "disabled",
                    "verification_status": "not_required",
                    "capabilities": ["proxy.chat"],
                }
            }
            views.append(
                AccountView(
                    provider=e.provider,
                    account_id=e.account_id,
                    label=e.account_id,
                    source="env",
                    enabled=not e.shadowed,
                    summary_status="disabled" if e.shadowed else "active",
                    purposes=purposes,
                    masked_identity=_mask_secret(e.secret),
                    shadowed=e.shadowed,
                )
            )
        for d in sorted(self._dyn.values(), key=lambda x: (x.provider, x.account_id)):
            views.append(
                AccountView(
                    provider=d.provider,
                    account_id=d.account_id,
                    label=d.label,
                    source=d.source,
                    enabled=d.enabled,
                    summary_status=_summary_status(enabled=d.enabled, purposes=d.purposes),
                    purposes=dict(d.purposes),
                    masked_identity=d.masked_identity,
                    shadowed=False,
                    created_at=d.created_at,
                    updated_at=d.updated_at,
                )
            )
        return views

    def _ensure_built(self) -> None:
        if not self._built:
            raise RuntimeError("registry not built; call rebuild() first")
