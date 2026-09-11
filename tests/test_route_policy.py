"""Routing policy: priority tiers, intra-tier weights, validation."""

from __future__ import annotations

from collections import Counter

import pytest

from qb2api.route_policy import (
    RoutePolicy,
    RouteScheduler,
    default_policies,
    normalize_weights,
    parse_policies,
    policies_to_payload,
    validate_policies,
)

PROVIDERS = ("codebuddy", "workbuddy_intl")


def _first_counts(policies: dict[str, RoutePolicy], rounds: int = 40) -> Counter:
    scheduler = RouteScheduler(policies)
    return Counter(
        scheduler.order(("codebuddy", "workbuddy_intl"))[0] for _ in range(rounds)
    )


def test_default_policies_are_one_equal_tier():
    policies = default_policies(PROVIDERS)
    assert set(policies) == set(PROVIDERS)
    assert {p.priority for p in policies.values()} == {0}
    assert {p.weight for p in policies.values()} == {1}
    assert all(p.enabled for p in policies.values())


def test_default_order_is_plain_round_robin():
    counts = _first_counts(default_policies(PROVIDERS))
    assert counts == {"codebuddy": 20, "workbuddy_intl": 20}


def test_lower_priority_always_wins_and_others_follow():
    scheduler = RouteScheduler({
        "workbuddy_intl": RoutePolicy("workbuddy_intl", priority=0),
        "codebuddy": RoutePolicy("codebuddy", priority=1),
    })
    for _ in range(5):
        assert scheduler.order(("codebuddy", "workbuddy_intl")) == (
            "workbuddy_intl",
            "codebuddy",
        )


def test_weight_controls_the_share_inside_one_tier():
    counts = _first_counts({
        "workbuddy_intl": RoutePolicy("workbuddy_intl", weight=3),
        "codebuddy": RoutePolicy("codebuddy", weight=1),
    })
    assert counts == {"workbuddy_intl": 30, "codebuddy": 10}


def test_disabled_route_is_offered_last():
    scheduler = RouteScheduler({
        "workbuddy_intl": RoutePolicy("workbuddy_intl", enabled=False),
        "codebuddy": RoutePolicy("codebuddy"),
    })
    assert scheduler.order(("workbuddy_intl", "codebuddy")) == ("codebuddy", "workbuddy_intl")


def test_zero_weight_route_is_offered_last():
    scheduler = RouteScheduler({
        "workbuddy_intl": RoutePolicy("workbuddy_intl", weight=0),
        "codebuddy": RoutePolicy("codebuddy", weight=1),
    })
    assert scheduler.order(("workbuddy_intl", "codebuddy")) == ("codebuddy", "workbuddy_intl")


def test_order_drops_candidates_without_a_live_pool():
    scheduler = RouteScheduler(default_policies(PROVIDERS))
    assert scheduler.order(("codebuddy",)) == ("codebuddy",)


def test_normalize_weights_zeroes_disabled_routes():
    normalized = normalize_weights({
        "workbuddy_intl": RoutePolicy("workbuddy_intl", weight=5, enabled=False),
        "codebuddy": RoutePolicy("codebuddy", weight=2),
    })
    assert normalized["workbuddy_intl"].weight == 0
    assert normalized["codebuddy"].weight == 2


def test_parse_policies_fills_missing_providers_with_defaults():
    parsed = parse_policies(
        [{"provider": "workbuddy_intl", "priority": 0, "weight": 7, "enabled": True}],
        PROVIDERS,
    )
    assert parsed["workbuddy_intl"] == RoutePolicy("workbuddy_intl", 0, 7, True)
    assert parsed["codebuddy"] == RoutePolicy("codebuddy", 0, 1, True)


def test_payload_round_trip_is_stable():
    policies = {
        "workbuddy_intl": RoutePolicy("workbuddy_intl", priority=0, weight=4),
        "codebuddy": RoutePolicy("codebuddy", priority=2, weight=1, enabled=False),
    }
    payload = policies_to_payload(policies)
    assert payload[0]["provider"] == "workbuddy_intl"
    assert parse_policies(payload, PROVIDERS) == policies


@pytest.mark.parametrize(
    "bad",
    [
        [{"provider": "unknown", "priority": 0, "weight": 1, "enabled": True}],
        [{"provider": "codebuddy", "priority": -1, "weight": 1, "enabled": True}],
        [{"provider": "codebuddy", "priority": 0, "weight": 100000, "enabled": True}],
        [{"provider": "codebuddy", "priority": 0, "weight": 1, "enabled": "yes"}],
        [{"provider": "", "priority": 0, "weight": 1, "enabled": True}],
        "not-a-list",
    ],
)
def test_parse_policies_rejects_malformed_input(bad):
    with pytest.raises(ValueError):
        parse_policies(bad, PROVIDERS)


def test_validate_rejects_all_disabled_and_all_zero_weight():
    with pytest.raises(ValueError):
        validate_policies(
            [
                {"provider": "codebuddy", "enabled": False},
                {"provider": "workbuddy_intl", "enabled": False},
            ],
            PROVIDERS,
        )
    with pytest.raises(ValueError):
        validate_policies(
            [
                {"provider": "codebuddy", "weight": 0},
                {"provider": "workbuddy_intl", "weight": 0},
            ],
            PROVIDERS,
        )


def test_validate_accepts_a_single_enabled_route():
    policies = validate_policies(
        [
            {"provider": "workbuddy_intl", "priority": 0, "weight": 1, "enabled": True},
            {"provider": "codebuddy", "priority": 1, "weight": 0, "enabled": False},
        ],
        PROVIDERS,
    )
    assert policies["workbuddy_intl"].enabled is True
