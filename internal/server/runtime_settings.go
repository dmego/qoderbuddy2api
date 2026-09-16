package server

import (
	"context"
	"log/slog"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/checkin"
	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/growth"
	"github.com/dmego/qoderbuddy2api/internal/metrics"
	"github.com/dmego/qoderbuddy2api/internal/store"
)

// RuntimeSettings answers live setting reads for the schedulers.
//
// Values come from runtime_settings (written by PATCH /api/admin/settings) and
// fall back to the environment defaults; overrides are cached briefly so a
// scheduler tick does not issue a query per setting. Both the check-in and the
// growth/metrics interfaces are satisfied by this single type so the console has
// exactly one source of truth for a setting.
type RuntimeSettings struct {
	db       *store.DB
	settings config.Settings

	mu       sync.RWMutex
	cached   map[string]any
	loadedAt time.Time
	ttl      time.Duration
}

// NewRuntimeSettings builds the provider.
func NewRuntimeSettings(db *store.DB, settings config.Settings) *RuntimeSettings {
	return &RuntimeSettings{db: db, settings: settings, ttl: 5 * time.Second}
}

// refresh reloads the override map when the cache has expired.
func (r *RuntimeSettings) snapshot() map[string]any {
	r.mu.RLock()
	fresh := time.Since(r.loadedAt) < r.ttl && r.cached != nil
	cached := r.cached
	r.mu.RUnlock()
	if fresh {
		return cached
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	loaded, err := r.db.RuntimeSettingsMap(ctx)
	if err != nil {
		// Keep serving the last good snapshot rather than reverting every
		// scheduler to environment defaults on a transient read failure.
		slog.Warn("runtime settings reload failed", "error", err)
		if cached != nil {
			return cached
		}
		loaded = map[string]any{}
	}
	r.mu.Lock()
	r.cached = loaded
	r.loadedAt = time.Now()
	r.mu.Unlock()
	return loaded
}

func (r *RuntimeSettings) bool(key string, fallback bool) bool {
	if value, ok := r.snapshot()[key]; ok {
		if flag, ok := value.(bool); ok {
			return flag
		}
	}
	return fallback
}

func (r *RuntimeSettings) intValue(key string, fallback int) int {
	if value, ok := r.snapshot()[key]; ok {
		if number, ok := numeric(value); ok {
			return number
		}
	}
	return fallback
}

func (r *RuntimeSettings) stringValue(key, fallback string) string {
	if value, ok := r.snapshot()[key]; ok {
		if text, ok := value.(string); ok && strings.TrimSpace(text) != "" {
			return text
		}
	}
	return fallback
}

// ---- checkin.RuntimeSettings ----

func (r *RuntimeSettings) CheckinEnabled() bool {
	return r.bool("checkin.enabled", r.settings.CheckinEnabled)
}

func (r *RuntimeSettings) CheckinAt() string {
	return r.stringValue("checkin.at", r.settings.CheckinAt)
}

func (r *RuntimeSettings) CheckinTimezone() string {
	return r.stringValue("checkin.timezone", r.settings.CheckinTimezone)
}

func (r *RuntimeSettings) CheckinCatchUp() bool {
	return r.bool("checkin.catch_up", r.settings.CheckinCatchUp)
}

func (r *RuntimeSettings) CheckinCatchUpWindowHours() int {
	return r.intValue("checkin.catch_up_window_hours", r.settings.CheckinCatchUpHours)
}

func (r *RuntimeSettings) CheckinJitterMinSeconds() int {
	return r.intValue("checkin.jitter_min_seconds", r.settings.CheckinJitterMin)
}

func (r *RuntimeSettings) CheckinJitterMaxSeconds() int {
	return r.intValue("checkin.jitter_max_seconds", r.settings.CheckinJitterMax)
}

func (r *RuntimeSettings) CheckinRetryLimit() int {
	return r.intValue("checkin.retry_limit", r.settings.CheckinRetryLimit)
}

// ---- growth.RuntimeSettings ----

func (r *RuntimeSettings) GrowthSchedulerEnabled() bool {
	return r.bool("growth.scheduler_enabled", r.settings.GrowthSchedulerEnabled)
}

func (r *RuntimeSettings) GrowthSchedulerIntervalSeconds() int {
	return r.intValue("growth.scheduler_interval_seconds", r.settings.GrowthSchedulerInterval)
}

func (r *RuntimeSettings) GrowthAutoTasks() bool {
	return r.bool("growth.auto_tasks", r.settings.GrowthAutoTasks)
}

func (r *RuntimeSettings) GrowthAutoLottery() bool {
	return r.bool("growth.auto_lottery", r.settings.GrowthAutoLottery)
}

func (r *RuntimeSettings) GrowthAutoTravel() bool {
	return r.bool("growth.auto_travel", r.settings.GrowthAutoTravel)
}

func (r *RuntimeSettings) GrowthAutoRedeem() bool {
	return r.bool("growth.auto_redeem", r.settings.GrowthAutoRedeem)
}

func (r *RuntimeSettings) GrowthRedeemTier() string {
	return r.stringValue("growth.redeem_tier", r.settings.GrowthRedeemTier)
}

func (r *RuntimeSettings) GrowthAutoBuddyOpen() bool {
	return r.bool("growth.auto_buddy_open", r.settings.GrowthAutoBuddyOpen)
}

func (r *RuntimeSettings) GrowthAutoActiveDay() bool {
	return r.bool("growth.auto_active_day", r.settings.GrowthAutoActiveDay)
}

func (r *RuntimeSettings) GrowthActiveDayConfirmAttempts() int {
	return r.intValue("growth.active_day_confirm_attempts", r.settings.GrowthActiveDayAttempts)
}

// ---- metrics / usage scheduler settings ----

func (r *RuntimeSettings) MetricsEnabled() bool {
	return r.bool("monitoring.metrics_enabled", r.settings.MetricsEnabled)
}

func (r *RuntimeSettings) MetricsIntervalSeconds() int {
	return r.intValue("monitoring.metrics_interval_seconds", r.settings.MetricsIntervalSeconds)
}

func (r *RuntimeSettings) UsageRollupIntervalSeconds() int {
	return r.intValue("usage.rollup_interval_seconds", r.settings.UsageRollupInterval)
}

func (r *RuntimeSettings) UsageDetailRetentionDays() int {
	return r.intValue("usage.detail_retention_days", r.settings.UsageDetailRetention)
}

// Compile-time proof that one provider satisfies every scheduler's interface.
var (
	_ checkin.RuntimeSettings = (*RuntimeSettings)(nil)
	_ growth.RuntimeSettings  = (*RuntimeSettings)(nil)
)

// describeSetting renders the override map for the service view.
func (r *RuntimeSettings) describeSetting(key string) string {
	return strconv.Quote(r.stringValue(key, ""))
}

var _ = metrics.KindCheckin
