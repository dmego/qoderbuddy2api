package growth

import (
	"context"
	"sync"
	"time"

	"github.com/dmego/qoderbuddy2api/gobackend/internal/config"
	"github.com/dmego/qoderbuddy2api/gobackend/internal/store"
)

// minSchedulerInterval floors the configured interval: each scheduled round
// spends real upstream turns, so a small value would be a cost bug rather than a
// responsiveness win.
const minSchedulerInterval = 600

// SchedulerOptions configures the growth scheduler.
type SchedulerOptions struct {
	Automation *Automation
	Settings   config.Settings
	Runtime    RuntimeSettings
}

// Scheduler owns the growth automation round timer.
type Scheduler struct {
	opts SchedulerOptions

	mu         sync.Mutex
	cancel     context.CancelFunc
	running    bool
	inCycle    bool
	lastRunAt  string
	lastError  string
	cycleCount int
}

// NewScheduler builds the scheduler.
func NewScheduler(opts SchedulerOptions) *Scheduler { return &Scheduler{opts: opts} }

func (s *Scheduler) enabled() bool {
	if s.opts.Runtime != nil {
		return s.opts.Runtime.GrowthSchedulerEnabled()
	}
	return s.opts.Settings.GrowthSchedulerEnabled
}

// intervalSeconds is the effective round interval.
func (s *Scheduler) intervalSeconds() int {
	configured := s.opts.Settings.GrowthSchedulerInterval
	if s.opts.Runtime != nil {
		configured = s.opts.Runtime.GrowthSchedulerIntervalSeconds()
	}
	if configured < minSchedulerInterval {
		return minSchedulerInterval
	}
	return configured
}

// Start launches the round loop. It runs one round immediately so a restart
// acts on today rather than waiting out a full interval.
func (s *Scheduler) Start() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.running || s.opts.Automation == nil {
		return
	}
	if !s.enabled() {
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	s.cancel = cancel
	s.running = true
	go s.loop(ctx)
}

// Stop halts the loop and waits for the in-flight round to finish.
func (s *Scheduler) Stop() {
	s.mu.Lock()
	cancel := s.cancel
	s.cancel = nil
	s.running = false
	s.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (s *Scheduler) loop(ctx context.Context) {
	for {
		if ctx.Err() != nil {
			return
		}
		s.runCycle(ctx)
		timer := time.NewTimer(time.Duration(s.intervalSeconds()) * time.Second)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}

// runCycle performs one round across every eligible domestic account.
func (s *Scheduler) runCycle(ctx context.Context) {
	if s.opts.Automation == nil {
		return
	}
	s.mu.Lock()
	if s.inCycle {
		s.mu.Unlock()
		return
	}
	s.inCycle = true
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.inCycle = false
		s.cycleCount++
		s.lastRunAt = store.NowISO()
		s.mu.Unlock()
	}()

	if s.opts.Automation.opts.Accounts == nil {
		return
	}
	refs := s.opts.Automation.opts.Accounts.EligibleForPurpose(Provider, "checkin")
	for _, ref := range refs {
		if ctx.Err() != nil {
			return
		}
		if ref.Provider != Provider {
			continue
		}
		s.runAccount(ctx, ref.AccountID)
	}
}

// runAccount executes and confirms one account. A single account's failure is
// recorded and does not stop the round.
func (s *Scheduler) runAccount(ctx context.Context, accountID string) {
	automation := s.opts.Automation
	token, err := automation.resolveToken(ctx, Provider, accountID)
	if err != nil {
		s.record("growth scheduler skip " + Provider + "/" + accountID + ": no token")
		return
	}
	localDate := automation.localDate()
	timezone := automation.timezone()
	results := automation.RunSteps(ctx, token, accountID, localDate, timezone)
	if _, err := automation.ConfirmActiveDay(ctx, Provider, accountID); err != nil {
		s.record(typeName(err))
	}
	if err := automation.RecordRun(ctx, Provider, accountID, "scheduler", results); err != nil {
		// The run already happened; a lost log row must not look like a failed
		// automation round.
		s.record(typeName(err))
	}
	automation.MaybeRefreshMetrics(ctx, Provider, accountID, results)
}

func (s *Scheduler) record(message string) {
	s.mu.Lock()
	s.lastError = message
	s.mu.Unlock()
}

// Status renders the scheduler snapshot for the service view.
func (s *Scheduler) Status() map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	var lastRunAt any
	if s.lastRunAt != "" {
		lastRunAt = s.lastRunAt
	}
	var lastError any
	if s.lastError != "" {
		lastError = s.lastError
	}
	return map[string]any{
		"enabled":          s.enabled(),
		"running":          s.running,
		"in_cycle":         s.inCycle,
		"interval_seconds": s.intervalSeconds(),
		"min_interval":     minSchedulerInterval,
		"cycles":           s.cycleCount,
		"last_run_at":      lastRunAt,
		"last_error":       lastError,
	}
}
