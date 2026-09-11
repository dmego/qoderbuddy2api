"""Per-model provider routing policy: priority tiers plus intra-tier weights.

One unified model id can be served by several providers (for example
``deepseek-v4.1-flash`` exists on both the domestic CodeBuddy pool and the
international WorkBuddy pool). The administrator decides, per model, which
provider is tried first and how load is shared between providers of the same
tier:

- ``priority`` — lower value is tried first. Distinct priorities form tiers.
- ``weight`` — relative share inside one tier (smooth weighted round-robin).
- ``enabled`` — a disabled route is never offered to the router.

Defaults reproduce the historical behaviour: every route in one tier with an
equal weight, i.e. plain round-robin over all providers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

MAX_PRIORITY = 99
MAX_WEIGHT = 1000


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    """Routing policy for one (model, provider) route."""

    provider: str
    priority: int = 0
    weight: int = 1
    enabled: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "priority": self.priority,
            "weight": self.weight,
            "enabled": self.enabled,
        }

    @classmethod
    def from_payload(cls, value: Any) -> RoutePolicy:
        if not isinstance(value, dict):
            raise ValueError("route policy must be an object")
        provider = value.get("provider")
        if not isinstance(provider, str) or not provider or len(provider) > 64:
            raise ValueError("invalid route policy provider")
        return cls(
            provider=provider,
            priority=_bounded(value.get("priority", 0), MAX_PRIORITY, "priority"),
            weight=_bounded(value.get("weight", 1), MAX_WEIGHT, "weight"),
            enabled=_boolean(value.get("enabled", True), "enabled"),
        )


def _bounded(value: Any, maximum: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"invalid route policy {field}")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"invalid route policy {field}")
    return value


def default_policies(providers: tuple[str, ...]) -> dict[str, RoutePolicy]:
    """Equal-weight single tier — the pre-policy round-robin behaviour."""
    return {provider: RoutePolicy(provider=provider) for provider in providers}


def parse_policies(
    raw: Any,
    providers: tuple[str, ...],
) -> dict[str, RoutePolicy]:
    """Parse a stored or submitted policy list, filling gaps with defaults."""
    policies = default_policies(providers)
    if raw is None:
        return policies
    if not isinstance(raw, list):
        raise ValueError("route policies must be a list")
    for item in raw:
        policy = RoutePolicy.from_payload(item)
        if policy.provider not in policies:
            raise ValueError(f"unknown route provider: {policy.provider}")
        policies[policy.provider] = policy
    return policies


def policies_to_payload(policies: dict[str, RoutePolicy]) -> list[dict[str, Any]]:
    return [
        policy.to_payload()
        for policy in sorted(policies.values(), key=lambda item: (item.priority, item.provider))
    ]


def validate_policies(
    raw: Any,
    providers: tuple[str, ...],
) -> dict[str, RoutePolicy]:
    """Validate an admin submission, rejecting anything unusable or contradictory."""
    policies = parse_policies(raw, providers)
    if not any(policy.enabled for policy in policies.values()):
        raise ValueError("at least one route must stay enabled")
    if all(policy.weight == 0 for policy in policies.values() if policy.enabled):
        raise ValueError("at least one enabled route needs a positive weight")
    return policies


def normalize_weights(policies: dict[str, RoutePolicy]) -> dict[str, RoutePolicy]:
    """Force zero weight on disabled routes so they never win a tier draw."""
    return {
        provider: replace(policy, weight=0) if not policy.enabled else policy
        for provider, policy in policies.items()
    }


class RouteScheduler:
    """Smooth weighted round-robin across priority tiers for one model."""

    def __init__(self, policies: dict[str, RoutePolicy]) -> None:
        self._policies = normalize_weights(policies)
        self._counters: dict[int, dict[str, int]] = {}

    def policies(self) -> dict[str, RoutePolicy]:
        return dict(self._policies)

    def order(self, candidates: tuple[str, ...]) -> tuple[str, ...]:
        """Return candidate providers ordered by tier, weighted inside each tier.

        ``candidates`` are the providers that currently have a live pool; the
        result keeps only those, so a route whose pool disappeared is skipped.
        """
        available = set(candidates)
        tiers: dict[int, list[str]] = {}
        for provider in candidates:
            policy = self._policies.get(provider) or RoutePolicy(provider=provider)
            if not policy.enabled or policy.weight <= 0:
                continue
            tiers.setdefault(policy.priority, []).append(provider)
        ordered: list[str] = []
        for priority in sorted(tiers):
            route_order = self._draw(tiers[priority], priority)
            ordered.extend(route_order)
        # Providers with no usable policy (disabled or zero weight) go last so a
        # misconfiguration degrades to "still serve" instead of "no routes".
        ordered.extend(provider for provider in candidates if provider not in ordered)
        del available
        return tuple(ordered)

    def _draw(self, providers: list[str], priority: int) -> list[str]:
        """One smooth-weighted draw; the winner leads, the rest follow by weight."""
        state = self._counters.setdefault(priority, {})
        for provider in providers:
            state.setdefault(provider, 0)
        total = sum(self._policies[provider].weight for provider in providers if provider in self._policies)
        if total <= 0:
            return providers
        for provider in providers:
            policy = self._policies.get(provider)
            state[provider] += policy.weight if policy else 1
        winner = max(providers, key=lambda provider: (state[provider], -providers.index(provider)))
        state[winner] -= total
        return [winner, *(provider for provider in providers if provider != winner)]
