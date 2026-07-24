"""Create account-scoped upstream providers without owning their lifecycle."""

from __future__ import annotations

from .accounts.resolver import CredentialResolver
from .config import Settings
from .providers.base import Provider
from .providers.codebuddy import CodeBuddyProvider
from .providers.qoder import QoderProvider


class ProviderFactory:
    def __init__(
        self,
        settings: Settings,
        resolver: CredentialResolver | None = None,
    ) -> None:
        self._settings = settings
        self._resolver = resolver

    def codebuddy_static(self, token: str) -> Provider:
        return CodeBuddyProvider(
            token=token,
            endpoint=self._settings.codebuddy_endpoint,
        )

    def codebuddy_dynamic(self, account_id: str) -> Provider:
        resolver = self._require_resolver()

        async def credential_getter() -> str:
            credential = await resolver.credential("codebuddy", account_id, "chat")
            return (
                credential.payload.get("access_token")
                or credential.payload.get("token")
                or ""
            )

        return CodeBuddyProvider(
            endpoint=self._settings.codebuddy_endpoint,
            credential_getter=credential_getter,
        )

    def qoder(self, pat: str) -> Provider:
        return QoderProvider(pat=pat, timeout=self._settings.qoder_timeout)

    def _require_resolver(self) -> CredentialResolver:
        if self._resolver is None:
            raise RuntimeError("dynamic provider creation requires a credential resolver")
        return self._resolver
