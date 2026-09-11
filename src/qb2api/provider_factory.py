"""Create account-scoped upstream providers without owning their lifecycle."""

from __future__ import annotations

from .accounts.resolver import CredentialResolver
from .config import Settings
from .providers.base import Provider
from .providers.codebuddy import CodeBuddyProvider
from .providers.orcaterm import OrcaTermProvider
from .providers.qoder import QoderProvider
from .providers.workbuddy_intl import WorkBuddyIntlProvider


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
            default_reasoning_effort=self._settings.codebuddy_default_reasoning_effort,
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
            default_reasoning_effort=self._settings.codebuddy_default_reasoning_effort,
        )

    def workbuddy_intl_static(self, token: str) -> Provider:
        return WorkBuddyIntlProvider(
            token=token,
            endpoint=self._settings.workbuddy_intl_endpoint,
            default_reasoning_effort=self._settings.workbuddy_intl_default_reasoning_effort,
        )

    def workbuddy_intl_dynamic(self, account_id: str) -> Provider:
        resolver = self._require_resolver()

        async def credential_getter() -> str:
            credential = await resolver.credential("workbuddy_intl", account_id, "chat")
            return (
                credential.payload.get("access_token")
                or credential.payload.get("token")
                or ""
            )

        return WorkBuddyIntlProvider(
            endpoint=self._settings.workbuddy_intl_endpoint,
            credential_getter=credential_getter,
            default_reasoning_effort=self._settings.workbuddy_intl_default_reasoning_effort,
        )

    def qoder(self, pat: str) -> Provider:
        return QoderProvider(pat=pat, timeout=self._settings.qoder_timeout)

    def orcaterm_static(self, token: str) -> Provider:
        return OrcaTermProvider(
            token=token,
            endpoint=self._settings.orcaterm_endpoint,
            user_id=self._settings.orcaterm_user_id,
            timeout=self._settings.orcaterm_timeout,
        )

    def orcaterm_dynamic(self, account_id: str) -> Provider:
        resolver = self._require_resolver()

        async def credential_getter() -> str:
            credential = await resolver.credential("orcaterm", account_id, "chat")
            return (
                credential.payload.get("access_token")
                or credential.payload.get("token")
                or ""
            )

        return OrcaTermProvider(
            endpoint=self._settings.orcaterm_endpoint,
            user_id=self._settings.orcaterm_user_id,
            credential_getter=credential_getter,
            timeout=self._settings.orcaterm_timeout,
        )

    def _require_resolver(self) -> CredentialResolver:
        if self._resolver is None:
            raise RuntimeError("dynamic provider creation requires a credential resolver")
        return self._resolver
