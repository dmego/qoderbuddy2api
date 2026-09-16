package metrics

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/config"
)

// RuntimeSettings supplies the live overrides written through the settings API.
// A nil Runtime is allowed and falls back to config.Settings.
type RuntimeSettings interface {
	MetricsEnabled() bool
	MetricsIntervalSeconds() int
	UsageRollupIntervalSeconds() int
	UsageDetailRetentionDays() int
}

// SchedulerOptions configures the collection loop.
type SchedulerOptions struct {
	Collector *Collector
	Settings  config.Settings
	Runtime   RuntimeSettings
}

// Scheduler runs the collector on a fixed cadence.
type Scheduler struct {
	opts SchedulerOptions

	mu      sync.Mutex
	cancel  context.CancelFunc
	done    chan struct{}
	running bool
	lastErr string
}

// NewScheduler builds the scheduler. It does not start it.
func NewScheduler(opts SchedulerOptions) *Scheduler {
	return &Scheduler{opts: opts, done: closedChannel()}
}

// Start launches the collection loop. It is idempotent.
func (s *Scheduler) Start() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.running || s.opts.Collector == nil {
		return
	}
	ctx, cancel := context.WithCancel(context.Background())
	s.cancel = cancel
	s.done = make(chan struct{})
	s.running = true
	go s.loop(ctx, s.done)
}

// Stop halts the loop and waits for the in-flight round to finish. It is
// idempotent.
func (s *Scheduler) Stop() {
	s.mu.Lock()
	if !s.running {
		s.mu.Unlock()
		return
	}
	cancel, done := s.cancel, s.done
	s.running = false
	s.cancel = nil
	s.mu.Unlock()
	cancel()
	<-done
}

// loop runs one round immediately, then one per interval.
//
// The immediate round matters: the Python original only collected on its timer,
// so a restart left the console showing snapshots from before the downtime for
// up to a full interval.
func (s *Scheduler) loop(ctx context.Context, done chan struct{}) {
	defer close(done)
	for {
		s.round(ctx)
		select {
		case <-ctx.Done():
			return
		case <-time.After(s.interval()):
		}
	}
}

// round performs one collection, bounded so a hung upstream cannot wedge the
// loop past its next tick.
func (s *Scheduler) round(ctx context.Context) {
	if !s.Enabled() {
		return
	}
	roundCtx, cancel := context.WithTimeout(ctx, 5*time.Minute)
	defer cancel()
	counts, err := s.opts.Collector.Collect(roundCtx)
	s.mu.Lock()
	switch {
	case err == nil:
		s.lastErr = ""
	case roundCtx.Err() != nil:
		s.lastErr = "interrupted"
		slog.Warn("metrics round interrupted", "counts", counts)
	default:
		s.lastErr = err.Error()
		slog.Warn("metrics round failed", "error", err)
	}
	s.mu.Unlock()
}

// lastError renders the last round's failure, or nil when it succeeded.
func (s *Scheduler) lastError() any {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.lastErr == "" {
		return nil
	}
	return s.lastErr
}

// Status renders the console's metrics block.
func (s *Scheduler) Status() map[string]any {
	s.mu.Lock()
	running := s.running
	s.mu.Unlock()
	return map[string]any{
		"enabled":             s.Enabled(),
		"running":             running,
		"refresh_in_progress": s.opts.Collector != nil && s.opts.Collector.RefreshInProgress(),
		"last_error":          s.lastError(),
		"backoff":             s.backoffSnapshot(),
	}
}

// backoffSnapshot renders the collector's retry state, tolerating a scheduler
// built without a collector so the status route never panics.
func (s *Scheduler) backoffSnapshot() []map[string]any {
	if s.opts.Collector == nil {
		return []map[string]any{}
	}
	return s.opts.Collector.BackoffSnapshot()
}

// Enabled reports whether collection is switched on.
func (s *Scheduler) Enabled() bool {
	if s.opts.Runtime != nil {
		return s.opts.Runtime.MetricsEnabled()
	}
	return s.opts.Settings.MetricsEnabled
}

// interval is the collection cadence, floored at 30s because a faster cadence
// would hammer the billing endpoints with no operator benefit.
func (s *Scheduler) interval() time.Duration {
	seconds := s.opts.Settings.MetricsIntervalSeconds
	if s.opts.Runtime != nil {
		seconds = s.opts.Runtime.MetricsIntervalSeconds()
	}
	if seconds < 30 {
		seconds = 30
	}
	return time.Duration(seconds) * time.Second
}

func closedChannel() chan struct{} {
	channel := make(chan struct{})
	close(channel)
	return channel
}
