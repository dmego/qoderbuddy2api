"""Account pool: vault, repository, registry, resolver, and domain models."""

from .models import (
    AccountRecord,
    AccountSlot,
    AccountSource,
    AccountView,
    Credential,
    CredentialMode,
    CredentialRecord,
    ProviderName,
    PurposeName,
    PurposeRecord,
    PurposeStatus,
    VerificationStatus,
)
from .promote import promote_env_account
from .registry import AccountRegistry
from .repository import AccountRepository, CredentialVersionConflict
from .resolver import CredentialResolver
from .vault import CredentialVault

__all__ = [
    "AccountRecord",
    "AccountRegistry",
    "AccountRepository",
    "AccountSlot",
    "AccountSource",
    "AccountView",
    "Credential",
    "CredentialMode",
    "CredentialRecord",
    "CredentialResolver",
    "CredentialVault",
    "CredentialVersionConflict",
    "ProviderName",
    "PurposeName",
    "PurposeRecord",
    "PurposeStatus",
    "VerificationStatus",
    "promote_env_account",
]
