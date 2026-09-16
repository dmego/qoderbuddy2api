package checkin

import (
	"context"
	"sort"
)

// quotaSnapshot reads the current package balances for one account.
//
// A snapshot is only usable when it is fresh: a stale one would be recorded as
// the "before" side of a delta and produce a meaningless change.
func (s *Service) quotaSnapshot(ctx context.Context, target AccountRef) map[string]any {
	snapshots, err := s.options.DB.ListMetricSnapshots(ctx, target.Provider, target.AccountID)
	if err != nil {
		return nil
	}
	for _, snapshot := range snapshots {
		if snapshot.MetricKind != "points" || snapshot.Status != "fresh" {
			continue
		}
		value, ok := snapshot.Value.(map[string]any)
		if !ok {
			continue
		}
		observedAt := snapshot.ObservedAt
		return map[string]any{
			"packages":    quotaPackages(value),
			"observed_at": observedAt,
			"status":      snapshot.Status,
		}
	}
	return nil
}

// observeQuota re-reads the balance after a claim and records what changed.
func (s *Service) observeQuota(ctx context.Context, target AccountRef, result *Result, before map[string]any) {
	result.QuotaBefore = before
	if s.options.Metrics == nil {
		status := "claimed_balance_pending"
		result.QuotaChangeStatus = &status
		return
	}
	after, err := s.options.Metrics.RefreshOne(ctx, target.Provider, target.AccountID)
	if err != nil {
		status := "claimed_balance_pending"
		result.QuotaChangeStatus = &status
		return
	}
	result.QuotaAfter = map[string]any{
		"packages":    quotaPackages(after),
		"observed_at": nil,
	}
	if observed, ok := after["observed_at"].(string); ok && observed != "" {
		result.QuotaAfter["observed_at"] = observed
		result.QuotaObservedAt = &observed
	}
	result.QuotaDelta = quotaDelta(before, result.QuotaAfter)
	status := quotaChangeStatus(result.Outcome, result.QuotaDelta)
	result.QuotaChangeStatus = &status
}

// quotaChangeStatus classifies the observed balance movement.
func quotaChangeStatus(outcome string, delta map[string]any) string {
	if outcome == OutcomeAlreadyCheckedIn {
		return "already_checked_in"
	}
	if outcome != OutcomeClaimed {
		return "claimed_balance_pending"
	}
	packages, _ := delta["packages"].([]any)
	for _, raw := range packages {
		entry, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if value, ok := entry["delta"].(float64); ok && value > 0 {
			return "claimed_balance_increased"
		}
	}
	return "claimed_balance_unchanged"
}

// quotaDelta compares two snapshots package by package. Only packages present in
// BOTH sides with a numeric remaining value produce a delta; a package that
// appeared or vanished is not a change the operator can act on.
func quotaDelta(before, after map[string]any) map[string]any {
	beforePackages := packageMap(before)
	afterPackages := packageMap(after)
	names := map[string]bool{}
	for name := range beforePackages {
		names[name] = true
	}
	for name := range afterPackages {
		names[name] = true
	}
	ordered := make([]string, 0, len(names))
	for name := range names {
		ordered = append(ordered, name)
	}
	sort.Strings(ordered)

	entries := []any{}
	for _, name := range ordered {
		old, oldOK := beforePackages[name]
		current, newOK := afterPackages[name]
		if !oldOK || !newOK {
			continue
		}
		delta, ok := numericValue(current - old)
		if !ok {
			continue
		}
		entries = append(entries, map[string]any{"name": name, "delta": delta})
	}
	if len(entries) == 0 {
		return nil
	}
	return map[string]any{"packages": entries}
}

// packageMap extracts name → remaining from a snapshot's packages array.
func packageMap(snapshot map[string]any) map[string]float64 {
	out := map[string]float64{}
	if snapshot == nil {
		return out
	}
	packages, _ := snapshot["packages"].([]any)
	for _, raw := range packages {
		entry, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		name, _ := entry["name"].(string)
		if name == "" {
			continue
		}
		if remaining, ok := numericValue(entry["remaining"]); ok {
			out[name] = remaining
		}
	}
	return out
}

// quotaPackages normalises a credits payload into a package list.
//
// The upstream shapes differ per deployment, so this falls back through the
// nested forms rather than assuming one layout.
func quotaPackages(value map[string]any) []any {
	if value == nil {
		return []any{}
	}
	if packages, ok := value["packages"].([]any); ok {
		return packages
	}
	out := []any{}
	for _, pair := range []struct{ name, key string }{
		{"user_quota", "user_quota"},
		{"add_on_quota", "add_on_quota"},
	} {
		nested, ok := value[pair.key].(map[string]any)
		if !ok {
			continue
		}
		entry := map[string]any{"name": pair.name}
		for _, field := range []string{"remaining", "total", "unit", "expires_at"} {
			if fieldValue, ok := nested[field]; ok {
				entry[field] = fieldValue
			}
		}
		if len(entry) > 1 {
			out = append(out, entry)
		}
	}
	if len(out) > 0 {
		return out
	}
	if total, ok := numericValue(value["total_remaining"]); ok {
		return []any{map[string]any{"name": "total", "remaining": total}}
	}
	return out
}
