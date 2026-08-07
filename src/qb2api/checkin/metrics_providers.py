"""Provider-specific metric collection (Qoder quota / activity / CodeBuddy credits)."""

from __future__ import annotations

from typing import Any

from ..providers.qoder_auth import QoderSession
from .codebuddy_credits import CodeBuddyCreditsUnavailableError
from .quota import QuotaUnavailableError, normalize_quota


class ProviderMetricCollectorMixin:
    """Collect upstream provider metrics through snapshot write primitives."""

    async def _write_provider_snapshot(
        self,
        *,
        provider: str,
        account_id: str,
        purpose: str,
        state: Any,
    ) -> None:
        if provider == "qoder":
            key = (provider, account_id, "quota")
            if key in state.seen:
                return
            await self._write_quota_snapshot(account_id, state)
            await self._write_activity_snapshot(account_id, state)
        elif provider == "codebuddy":
            key = (provider, account_id, "points")
            if key in state.seen:
                return
            await self._write_credits_snapshot(account_id, state)

    async def _write_quota_snapshot(self, account_id: str, state: Any) -> None:
        key = ("qoder", account_id, "quota")
        state.seen.add(key)
        if await self._write_backoff_snapshot(key, state):
            return
        try:
            credential = await self._dependencies.resolver.credential(
                "qoder", account_id, "checkin"
            )
            token = await _qoder_access_token(credential)
            value = normalize_quota(await self._dependencies.qoder_quota.fetch(token))
        except (LookupError, QuotaUnavailableError) as error:
            await self._write_failure(key, state, str(error))
        except Exception as error:
            await self._write_failure(key, state, type(error).__name__)
        else:
            value = _preserve_reward_packages(value, state.previous.get(key, {}).get("value"))
            await self._write(key=key, value=value, status="fresh", state=state)
            self._backoff.pop(self._backoff_key(key), None)

    async def _write_credits_snapshot(self, account_id: str, state: Any) -> None:
        key = ("codebuddy", account_id, "points")
        state.seen.add(key)
        if await self._write_backoff_snapshot(key, state):
            return
        try:
            credential = await self._dependencies.resolver.credential(
                "codebuddy", account_id, "checkin"
            )
            token = _access_token(credential)
            value = await self._dependencies.codebuddy_credits.fetch(token)
        except (LookupError, QuotaUnavailableError, CodeBuddyCreditsUnavailableError) as error:
            await self._write_failure(key, state, str(error))
        except Exception as error:
            await self._write_failure(key, state, type(error).__name__)
        else:
            await self._write(key=key, value=value, status="fresh", state=state)
            self._backoff.pop(self._backoff_key(key), None)

    async def _write_activity_snapshot(self, account_id: str, state: Any) -> None:
        client = self._dependencies.qoder_activity
        if client is None:
            return
        key = ("qoder", account_id, "activity")
        state.seen.add(key)
        if await self._write_backoff_snapshot(key, state):
            return
        try:
            pat = await _qoder_pat(self._dependencies.resolver, account_id)
            value = {"activities": await client.fetch(pat)}
        except (LookupError, QuotaUnavailableError) as error:
            await self._write_failure(key, state, str(error))
        except Exception as error:
            await self._write_failure(key, state, type(error).__name__)
        else:
            await self._write(key=key, value=value, status="fresh", state=state)
            self._backoff.pop(self._backoff_key(key), None)


def _access_token(credential: Any) -> str:
    payload = credential.payload
    token = payload.get("access_token") or payload.get("device_token") or payload.get("token")
    if not isinstance(token, str) or not token.strip():
        raise QuotaUnavailableError("access token unavailable")
    return token.strip()


def _preserve_reward_packages(
    value: dict[str, Any], previous: Any,
) -> dict[str, Any]:
    """Retain locally observed Qoder earned packages when upstream omits them."""
    earned = _reward_packages(previous)
    if not earned:
        return value
    value["packages"] = _merge_packages(value.get("packages"), earned)
    return value


def _reward_packages(previous: Any) -> list[dict[str, Any]]:
    if not isinstance(previous, dict) or not isinstance(previous.get("packages"), list):
        return []
    return [
        package for package in previous["packages"]
        if isinstance(package, dict) and _is_reward_package(package)
    ]


def _merge_packages(current: Any, earned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    packages = [item for item in current if isinstance(item, dict)] if isinstance(current, list) else []
    keys = {_package_key(item) for item in packages}
    packages.extend(item for item in earned if _package_key(item) not in keys)
    return packages


def _is_reward_package(package: dict[str, Any]) -> bool:
    name = str(package.get("name", ""))
    return "reward" in name.lower() or "签到" in name or "奖励" in name


def _package_key(package: dict[str, Any]) -> tuple[Any, ...]:
    return (package.get("name"), package.get("total"), package.get("remaining"), package.get("expires_at"))


async def _qoder_access_token(credential: Any) -> str:
    """Resolve a quota bearer token, deriving it from a stored Qoder PAT."""
    payload = credential.payload
    token = payload.get("access_token") or payload.get("device_token") or payload.get("token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    pat = payload.get("pat")
    if not isinstance(pat, str) or not pat.strip():
        raise QuotaUnavailableError("access token unavailable")
    session = QoderSession(pat.strip())
    try:
        await session.authenticate()
        token = session.security_oauth_token
    finally:
        await session.close()
    if not isinstance(token, str) or not token.strip():
        raise QuotaUnavailableError("qoder oauth token unavailable")
    return token.strip()


async def _qoder_pat(resolver: Any, account_id: str) -> str:
    """Resolve the PAT for a Qoder account, preferring chat credential."""
    for purpose in ("checkin", "chat"):
        try:
            credential = await resolver.credential("qoder", account_id, purpose)
        except LookupError:
            continue
        pat = credential.payload.get("pat")
        if isinstance(pat, str) and pat.strip():
            return pat.strip()
    raise LookupError(f"no qoder PAT for {account_id}")
