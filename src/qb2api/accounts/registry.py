"""Merge env static slots with DB dynamic accounts; publish purpose snapshots."""

from __future__ import annotations

from typing import Any

from .models import AccountSlot, AccountView
from .registry_loader import DynamicSlot, EnvSlot, load_dynamic_slots, load_environment, mark_shadowed
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
        self._env: list[EnvSlot] = []
        self._dyn: dict[tuple[str, str], DynamicSlot] = {}
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
        env, env_secrets = load_environment(
            codebuddy_tokens=self._codebuddy_tokens,
            qoder_tokens=self._qoder_tokens,
        )
        dyn = await load_dynamic_slots(self._repo, self._vault)
        mark_shadowed(env, dyn)

        self._env = env
        self._dyn = dyn
        self._env_secrets = env_secrets
        self._built = True

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
        slots = _environment_snapshot(self._env) if purpose == "chat" else []
        slots.extend(_dynamic_snapshot(self._dyn.values(), purpose=purpose))
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


def _environment_snapshot(environment: list[EnvSlot]) -> list[AccountSlot]:
    return [
        AccountSlot(
            provider=slot.provider,
            account_id=slot.account_id,
            source="env",
            enabled=True,
            status="active",
            verification_status="not_required",
            shadowed=False,
            capabilities=["proxy.chat"],
        )
        for slot in environment
        if not slot.shadowed
    ]


def _dynamic_snapshot(slots: Any, *, purpose: str) -> list[AccountSlot]:
    return [
        account_slot
        for slot in sorted(slots, key=lambda item: (item.provider, item.account_id))
        if (account_slot := _slot_for_purpose(slot, purpose=purpose)) is not None
    ]


def _slot_for_purpose(slot: DynamicSlot, *, purpose: str) -> AccountSlot | None:
    purpose_state = slot.purposes.get(purpose)
    if not slot.enabled or not purpose_state or not purpose_state.get("enabled"):
        return None
    if purpose == "chat" and purpose_state.get("status") not in _CHAT_OK_STATUS:
        return None
    if purpose == "checkin" and not _verified_active(purpose_state):
        return None
    return AccountSlot(
        provider=slot.provider,
        account_id=slot.account_id,
        source=slot.source,
        enabled=True,
        status=purpose_state["status"],
        verification_status=purpose_state["verification_status"],
        shadowed=False,
        capabilities=list(purpose_state.get("capabilities") or []),
    )


def _verified_active(purpose_state: dict[str, Any]) -> bool:
    return (
        purpose_state.get("status") == "active"
        and purpose_state.get("verification_status") == "verified"
    )
