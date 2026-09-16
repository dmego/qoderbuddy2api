package growth

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/config"
)

// cycleLister counts how many times the scheduler asked for the account set,
// which is one per round.
type cycleLister struct {
	cycles atomic.Int32
}

func (l *cycleLister) EligibleForPurpose(provider, purpose string) []AccountRef {
	l.cycles.Add(1)
	return nil
}

func (l *cycleLister) Rebuild(context.Context) error { return nil }

// The scheduler must run one round immediately on Start rather than waiting out
// a full interval, and Stop must halt the loop.
func TestSchedulerRunsImmediately(t *testing.T) {
	lister := &cycleLister{}
	automation := NewAutomation(AutomationOptions{
		Settings: config.Settings{GrowthSchedulerInterval: 1800},
		Accounts: lister,
	})
	scheduler := NewScheduler(SchedulerOptions{
		Automation: automation,
		Settings:   config.Settings{GrowthSchedulerEnabled: true, GrowthSchedulerInterval: 1800},
	})
	scheduler.Start()
	// The interval is 30 minutes, so any cycle observed within this window
	// proves the loop runs before its first sleep.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) && lister.cycles.Load() == 0 {
		time.Sleep(10 * time.Millisecond)
	}
	scheduler.Stop()

	if got := lister.cycles.Load(); got < 1 {
		t.Fatalf("cycles = %d, want at least one immediate round", got)
	}
	if status := scheduler.Status(); status["running"] != false {
		t.Fatalf("running after Stop = %v, want false", status["running"])
	}
	if status := scheduler.Status(); status["last_run_at"] == nil {
		t.Fatal("last_run_at = nil, want the completed round recorded")
	}
	// A stopped scheduler must not keep cycling.
	settled := lister.cycles.Load()
	time.Sleep(150 * time.Millisecond)
	if lister.cycles.Load() != settled {
		t.Fatalf("cycles advanced after Stop: %d -> %d", settled, lister.cycles.Load())
	}
}

// The interval is floored so a misconfigured small value cannot turn scheduled
// upstream turns into a cost bug.
func TestSchedulerIntervalFloor(t *testing.T) {
	automation := &Automation{}
	cases := []struct {
		name     string
		settings config.Settings
		runtime  RuntimeSettings
		want     int
	}{
		{
			name:     "below the floor is raised",
			settings: config.Settings{GrowthSchedulerInterval: 30},
			want:     minSchedulerInterval,
		},
		{
			name:     "above the floor is kept",
			settings: config.Settings{GrowthSchedulerInterval: 3600},
			want:     3600,
		},
		{
			name:     "runtime override wins over the environment default",
			settings: config.Settings{GrowthSchedulerInterval: 3600},
			runtime:  &fakeRuntime{tier: "14d"},
			want:     1800,
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			scheduler := NewScheduler(SchedulerOptions{
				Automation: automation,
				Settings:   testCase.settings,
				Runtime:    testCase.runtime,
			})
			if got := scheduler.intervalSeconds(); got != testCase.want {
				t.Fatalf("intervalSeconds = %d, want %d", got, testCase.want)
			}
		})
	}
}

// Start is a no-op while the scheduler is disabled or unconfigured, and Stop is
// safe before Start.
func TestSchedulerStartDisabled(t *testing.T) {
	NewScheduler(SchedulerOptions{Settings: config.Settings{GrowthSchedulerEnabled: false}}).Start()
	disabled := NewScheduler(SchedulerOptions{
		Automation: &Automation{},
		Settings:   config.Settings{GrowthSchedulerEnabled: false},
	})
	disabled.Start()
	if status := disabled.Status(); status["running"] != false {
		t.Fatalf("disabled scheduler running = %v, want false", status["running"])
	}
	disabled.Stop()

	// A nil runtime must not be dereferenced anywhere on the enabled path.
	enabled := NewScheduler(SchedulerOptions{
		Automation: &Automation{},
		Settings:   config.Settings{GrowthSchedulerEnabled: true, GrowthSchedulerInterval: 1800},
	})
	if enabled.Status()["enabled"] != true {
		t.Fatalf("enabled = %v, want true", enabled.Status()["enabled"])
	}
}

// Runtime overrides must win, and a nil Runtime must fall back to Settings
// without panicking.
func TestAutomationSettingFallbacks(t *testing.T) {
	settings := config.Settings{
		GrowthAutoTasks: true, GrowthAutoLottery: false, GrowthAutoTravel: true,
		GrowthAutoRedeem: false, GrowthRedeemTier: "28d", GrowthAutoBuddyOpen: true,
		GrowthAutoActiveDay: false, GrowthActiveDayAttempts: 5,
	}
	fromSettings := NewAutomation(AutomationOptions{Settings: settings})
	if !fromSettings.autoTasks() || fromSettings.autoLottery() || !fromSettings.autoTravel() {
		t.Fatal("nil runtime must use the environment defaults")
	}
	if fromSettings.autoRedeem() || !fromSettings.autoBuddyOpen() || fromSettings.autoActiveDay() {
		t.Fatal("nil runtime must use the environment defaults")
	}
	if fromSettings.redeemTier() != "28d" || fromSettings.activeDayAttempts() != 5 {
		t.Fatalf("tier = %q, attempts = %d", fromSettings.redeemTier(), fromSettings.activeDayAttempts())
	}

	overridden := NewAutomation(AutomationOptions{
		Settings: settings,
		Runtime:  &fakeRuntime{tier: "7d", attempts: 1, autoFlags: true},
	})
	if !overridden.autoLottery() || !overridden.autoTasks() {
		t.Fatal("runtime overrides must win over the environment defaults")
	}
	if !overridden.autoRedeem() || !overridden.autoBuddyOpen() || !overridden.autoActiveDay() {
		t.Fatal("runtime overrides must win over the environment defaults")
	}
	if overridden.redeemTier() != "7d" || overridden.activeDayAttempts() != 1 {
		t.Fatalf("tier = %q, attempts = %d", overridden.redeemTier(), overridden.activeDayAttempts())
	}

	// A blank tier must not reach the upstream as an unknown tier.
	blank := NewAutomation(AutomationOptions{Settings: config.Settings{}})
	if blank.redeemTier() != "14d" {
		t.Fatalf("blank tier = %q, want the 14d default", blank.redeemTier())
	}
	// An attempt budget below one would make the first confirmation conclude
	// the day, so it is clamped.
	zero := NewAutomation(AutomationOptions{Settings: config.Settings{}, Runtime: &fakeRuntime{tier: "14d"}})
	if zero.activeDayAttempts() != 1 {
		t.Fatalf("attempts = %d, want 1", zero.activeDayAttempts())
	}
	// A blank timezone must not yield an empty local date.
	if got := zero.localDate(); len(got) != 10 {
		t.Fatalf("localDate = %q, want a YYYY-MM-DD date", got)
	}
	if zero.timezone() != "Asia/Shanghai" {
		t.Fatalf("timezone = %q, want Asia/Shanghai", zero.timezone())
	}
}

// A run with a successful read must still keep every step key, and a missing
// active-day context is a skip rather than an error or a silent absence.
func TestRunStepsKeepsEveryKeyOnFailure(t *testing.T) {
	server := newFakeGrowthServer(t, map[string]map[string]any{
		"/v2/activity/growth/profile":      map[string]any{},
		"/v2/activity/growth/tasks":        map[string]any{"tasks": []any{}},
		"/activity/growth/heatmap":         map[string]any{"cells": []any{}},
		"/activity/growth/streak":          map[string]any{},
		"/activity/growth/lottery/summary": map[string]any{},
	})
	automation, _ := newTestAutomation(t, nil)
	automation.opts.GrowthClient = NewClient(server.URL, 0)
	automation.settings.GrowthAutoTasks = true
	automation.settings.GrowthAutoActiveDay = true
	results := automation.RunSteps(context.Background(), "token", "", "", "")
	if len(results) != len(StepKeys) {
		t.Fatalf("results = %d, want %d", len(results), len(StepKeys))
	}
	active := results[StepActiveDay]
	if active.Status != "skipped" || active.Detail != "active_day_context_missing" {
		t.Fatalf("active_day = %+v, want the context-missing skip", active)
	}
}
