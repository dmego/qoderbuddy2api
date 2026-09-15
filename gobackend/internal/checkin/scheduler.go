package checkin

import (
	"context"
	"log/slog"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/dmego/qoderbuddy2api/gobackend/internal/store"
)

// Scheduler owns the daily timer and the catch-up decision.
type Scheduler struct {
	options SchedulerOptions

	mu              sync.Mutex
	stopped         bool
	done            chan struct{}
	lastRunAt       string
	lastError       string
	catchUpDecision string
}

// NewScheduler builds the scheduler.
func NewScheduler(options SchedulerOptions) *Scheduler {
	return &Scheduler{options: options, catchUpDecision: "not_evaluated"}
}

// Start launches the scheduler loop. A disabled schedule is a no-op, which keeps
// the status view honest rather than reporting a timer that never fires.
func (s *Scheduler) Start() {
	s.mu.Lock()
	if s.done != nil || s.stopped {
		s.mu.Unlock()
		return
	}
	if !s.enabled() {
		s.mu.Unlock()
		slog.Info("checkin scheduler disabled")
		return
	}
	s.done = make(chan struct{})
	done := s.done
	s.mu.Unlock()
	go s.loop(done)
}

// Stop halts the scheduler loop.
func (s *Scheduler) Stop() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.stopped = true
	if s.done != nil {
		close(s.done)
		s.done = nil
	}
}

// Reconfigure restarts the timer with the current settings.
func (s *Scheduler) Reconfigure() {
	s.mu.Lock()
	previous := s.done
	s.done = nil
	s.stopped = false
	s.catchUpDecision = "not_evaluated"
	s.lastError = ""
	s.mu.Unlock()
	if previous != nil {
		close(previous)
	}
	s.Start()
}

// loop runs catch-up once before sleeping, so a restart repairs a missed run
// immediately instead of waiting a full day.
func (s *Scheduler) loop(done chan struct{}) {
	s.maybeCatchUp(done)
	for {
		delay := s.durationUntilNextRun()
		timer := time.NewTimer(delay)
		select {
		case <-done:
			timer.Stop()
			return
		case <-timer.C:
		}
		s.launch("scheduler")
	}
}

// durationUntilNextRun is the time to the next scheduled instant, with a floor
// so a schedule landing exactly on "now" still sleeps rather than spinning.
func (s *Scheduler) durationUntilNextRun() time.Duration {
	location, err := time.LoadLocation(s.timezone())
	if err != nil {
		location = time.UTC
	}
	hour, minute := parseClock(s.checkinAt())
	now := time.Now().In(location)
	next := time.Date(now.Year(), now.Month(), now.Day(), hour, minute, 0, 0, location)
	// Inclusive boundary: a candidate equal to now rolls to tomorrow.
	if !next.After(now) {
		next = next.AddDate(0, 0, 1)
	}
	delay := time.Until(next)
	if delay < 500*time.Millisecond {
		return 500 * time.Millisecond
	}
	return delay
}

// maybeCatchUp decides whether today's scheduled run was missed.
func (s *Scheduler) maybeCatchUp(done chan struct{}) {
	if !s.catchUpEnabled() {
		s.setDecision("disabled")
		return
	}
	location, err := time.LoadLocation(s.timezone())
	if err != nil {
		location = time.UTC
	}
	hour, minute := parseClock(s.checkinAt())
	now := time.Now().In(location)
	planned := time.Date(now.Year(), now.Month(), now.Day(), hour, minute, 0, 0, location)
	if now.Before(planned) {
		s.setDecision("before_schedule")
		return
	}
	window := time.Duration(s.catchUpWindowHours()) * time.Hour
	if now.Sub(planned) > window {
		s.setDecision("outside_window")
		slog.Info("checkin catch-up skipped: outside window")
		return
	}
	if !s.hasPendingTargets() {
		s.setDecision("already_complete")
		slog.Info("checkin catch-up skipped: all targets already terminal")
		return
	}
	jitter := s.jitterDuration()
	if jitter > 0 {
		select {
		case <-done:
			s.setDecision("stopped")
			return
		case <-time.After(jitter):
		}
	}
	s.setDecision("started")
	s.launch("catch_up")
}

// hasPendingTargets reports whether any eligible account still needs today's run.
func (s *Scheduler) hasPendingTargets() bool {
	if s.options.Service == nil || s.options.Service.options.Registry == nil {
		return false
	}
	targets := s.options.Service.options.Registry.EligibleForPurpose("codebuddy", "checkin")
	if len(targets) == 0 {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	for _, target := range targets {
		state, err := s.options.Service.options.DB.GetCheckinDailyState(ctx, target.Provider, target.AccountID,
			s.options.Service.localDate(), s.timezone())
		if err != nil || state.TerminalOutcome == nil {
			return true
		}
		outcome := *state.TerminalOutcome
		if outcome != OutcomeClaimed && outcome != OutcomeAlreadyCheckedIn {
			return true
		}
	}
	return false
}

// launch starts a batch, swallowing the in-progress error because a concurrent
// run already covers the same accounts.
func (s *Scheduler) launch(trigger string) {
	if s.options.Service == nil {
		return
	}
	skipAlreadyDone := trigger == "scheduler" || trigger == "catch_up"
	runID, err := s.options.Service.StartBatch(context.Background(), trigger, nil, skipAlreadyDone)
	s.mu.Lock()
	defer s.mu.Unlock()
	if err != nil {
		if err == ErrRunInProgress {
			s.lastError = "checkin_run_in_progress"
			slog.Info("checkin run already in progress")
			return
		}
		s.lastError = typeName(err)
		slog.Warn("checkin batch failed to start", "error", err)
		return
	}
	s.lastError = ""
	s.lastRunAt = store.FormatISO(time.Now())
	slog.Info("checkin batch started", "trigger", trigger, "run_id", runID)
}

// Status renders the scheduler snapshot.
func (s *Scheduler) Status() map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	status := map[string]any{
		"catch_up_decision": s.catchUpDecision,
		"active_run_id":     nil,
		"last_error":        nilable(s.lastError),
		"last_run_at":       nilable(s.lastRunAt),
		"next_run_at":       nil,
	}
	if s.options.Service != nil {
		status["active_run_id"] = nilable(s.options.Service.ActiveRunID())
	}
	if s.enabled() {
		location, err := time.LoadLocation(s.timezone())
		if err != nil {
			location = time.UTC
		}
		hour, minute := parseClock(s.checkinAt())
		now := time.Now().In(location)
		next := time.Date(now.Year(), now.Month(), now.Day(), hour, minute, 0, 0, location)
		if !next.After(now) {
			next = next.AddDate(0, 0, 1)
		}
		status["next_run_at"] = next.Format(time.RFC3339)
	}
	return status
}

func (s *Scheduler) setDecision(value string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.catchUpDecision = value
}

func (s *Scheduler) enabled() bool {
	if s.options.Runtime != nil {
		return s.options.Runtime.CheckinEnabled()
	}
	return s.options.Settings.CheckinEnabled
}

func (s *Scheduler) checkinAt() string {
	if s.options.Runtime != nil {
		return s.options.Runtime.CheckinAt()
	}
	return s.options.Settings.CheckinAt
}

func (s *Scheduler) timezone() string {
	if s.options.Runtime != nil {
		return s.options.Runtime.CheckinTimezone()
	}
	return s.options.Settings.CheckinTimezone
}

func (s *Scheduler) catchUpEnabled() bool {
	if s.options.Runtime != nil {
		return s.options.Runtime.CheckinCatchUp()
	}
	return s.options.Settings.CheckinCatchUp
}

func (s *Scheduler) catchUpWindowHours() int {
	if s.options.Runtime != nil {
		return s.options.Runtime.CheckinCatchUpWindowHours()
	}
	return s.options.Settings.CheckinCatchUpHours
}

func (s *Scheduler) jitterDuration() time.Duration {
	minimum, maximum := s.options.Settings.CheckinJitterMin, s.options.Settings.CheckinJitterMax
	if s.options.Runtime != nil {
		minimum = s.options.Runtime.CheckinJitterMinSeconds()
		maximum = s.options.Runtime.CheckinJitterMaxSeconds()
	}
	lower, upper := minimum, maximum
	if lower > upper {
		lower, upper = upper, lower
	}
	if upper <= 0 {
		return 0
	}
	return time.Duration(lower+randomInt(upper-lower+1)) * time.Second
}

// defaultCheckinHour/Minute is the documented fallback schedule (00:10).
const (
	defaultCheckinHour   = 0
	defaultCheckinMinute = 10
)

// parseClock reads an HH:MM schedule time.
//
// Malformed input falls back to the documented default rather than 00:00: a
// typo in CHECKIN_AT should not silently move the daily run to midnight, and
// returning a fixed value keeps the scheduler's behaviour predictable.
func parseClock(value string) (int, int) {
	parts := strings.Split(strings.TrimSpace(value), ":")
	if len(parts) != 2 {
		return defaultCheckinHour, defaultCheckinMinute
	}
	hour, err := strconv.Atoi(strings.TrimSpace(parts[0]))
	if err != nil || hour < 0 || hour > 23 {
		return defaultCheckinHour, defaultCheckinMinute
	}
	minute, err := strconv.Atoi(strings.TrimSpace(parts[1]))
	if err != nil || minute < 0 || minute > 59 {
		return defaultCheckinHour, defaultCheckinMinute
	}
	return hour, minute
}
