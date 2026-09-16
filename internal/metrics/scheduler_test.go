package metrics

import (
	"context"
	"testing"

	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/models"
)

// TestSchedulerStatusShape pins the metrics block the check-in status page
// renders, including that it reports the collector's real backoff state rather
// than an empty placeholder.
func TestSchedulerStatusShape(t *testing.T) {
	clients := &fakeClients{credits: creditPayload()}
	accounts := &fakeAccounts{byProvider: map[string][]AccountRef{
		models.ProviderWorkBuddy: {{Provider: models.ProviderWorkBuddy, AccountID: "cb-test"}},
	}}
	collector, _ := newTestCollector(t, clients, accounts, map[string]any{"access_token": "token-value"}, nil)
	scheduler := NewScheduler(SchedulerOptions{
		Collector: collector,
		Settings:  config.Settings{MetricsEnabled: true, MetricsIntervalSeconds: 900},
	})

	status := scheduler.Status()
	for _, key := range []string{"enabled", "running", "refresh_in_progress", "last_error", "backoff"} {
		if _, present := status[key]; !present {
			t.Fatalf("status is missing %q: %#v", key, status)
		}
	}
	if status["enabled"] != true {
		t.Fatalf("enabled = %#v, want true", status["enabled"])
	}
	if status["running"] != false {
		t.Fatalf("running = %#v, want false before Start", status["running"])
	}
	if status["last_error"] != nil {
		t.Fatalf("last_error = %#v, want nil", status["last_error"])
	}
	if entries, ok := status["backoff"].([]map[string]any); !ok || len(entries) != 0 {
		t.Fatalf("backoff = %#v, want an empty list", status["backoff"])
	}

	// A failing metric must surface here; this list is the operator's only view
	// of which metrics are cooling down.
	clients.creditsErr = errCreditsDown
	if _, err := collector.Collect(context.Background()); err != nil {
		t.Fatalf("collect: %v", err)
	}
	entries, ok := scheduler.Status()["backoff"].([]map[string]any)
	if !ok || len(entries) != 1 {
		t.Fatalf("backoff = %#v, want one entry", scheduler.Status()["backoff"])
	}
	entry := entries[0]
	if entry["metric"] != "codebuddy:cb-test:points" {
		t.Fatalf("metric = %#v", entry["metric"])
	}
	if entry["attempts"] != 1 {
		t.Fatalf("attempts = %#v, want 1", entry["attempts"])
	}
	if retryAt, ok := entry["retry_at"].(string); !ok || retryAt == "" {
		t.Fatalf("retry_at = %#v, want a timestamp", entry["retry_at"])
	}
}

// TestSchedulerStatusToleratesMissingCollector guards the status route against a
// scheduler built before its collector, which the console reaches during boot.
func TestSchedulerStatusToleratesMissingCollector(t *testing.T) {
	scheduler := NewScheduler(SchedulerOptions{Settings: config.Settings{MetricsEnabled: false}})
	status := scheduler.Status()
	if status["enabled"] != false {
		t.Fatalf("enabled = %#v, want false", status["enabled"])
	}
	if entries, ok := status["backoff"].([]map[string]any); !ok || len(entries) != 0 {
		t.Fatalf("backoff = %#v, want an empty list", status["backoff"])
	}
}

// TestSchedulerStartRunsARoundImmediately pins the startup behaviour: a restart
// must repair the console's view instead of waiting a full interval.
func TestSchedulerStartRunsARoundImmediately(t *testing.T) {
	clients := &fakeClients{credits: creditPayload()}
	accounts := &fakeAccounts{byProvider: map[string][]AccountRef{
		models.ProviderWorkBuddy: {{Provider: models.ProviderWorkBuddy, AccountID: "cb-test"}},
	}}
	collector, db := newTestCollector(t, clients, accounts, map[string]any{"access_token": "token-value"}, nil)
	scheduler := NewScheduler(SchedulerOptions{
		Collector: collector,
		Settings:  config.Settings{MetricsEnabled: true, MetricsIntervalSeconds: 3600},
	})
	scheduler.Start()
	t.Cleanup(scheduler.Stop)

	waitFor(t, func() bool {
		return len(findSnapshots(db, t, models.ProviderWorkBuddy, "cb-test", KindPoints)) == 1
	})

	if status := scheduler.Status(); status["running"] != true {
		t.Fatalf("running = %#v, want true while started", status["running"])
	}
	if accounts.rebuilds == 0 {
		t.Fatal("round did not rebuild the account list")
	}
	scheduler.Stop()
	if status := scheduler.Status(); status["running"] != false {
		t.Fatalf("running = %#v, want false after Stop", status["running"])
	}
}
