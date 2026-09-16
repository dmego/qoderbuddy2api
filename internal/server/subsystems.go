package server

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/checkin"
	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/growth"
	"github.com/dmego/qoderbuddy2api/internal/importer"
	"github.com/dmego/qoderbuddy2api/internal/metrics"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/oauthflow"
	"github.com/dmego/qoderbuddy2api/internal/store"
	"github.com/dmego/qoderbuddy2api/internal/vault"
)

// Subsystems owns every background component so shutdown is one call.
type Subsystems struct {
	CheckinScheduler *checkin.Scheduler
	GrowthScheduler  *growth.Scheduler
	MetricsScheduler *metrics.Scheduler
	RollupCancel     context.CancelFunc
	RotateCancel     context.CancelFunc

	mu      sync.Mutex
	stopped bool
}

// StartSubsystems wires and launches the scheduled work.
//
// Every subsystem tolerates being disabled by settings; a disabled component is
// left nil and its scheduler is simply not started, which keeps the console's
// status views honest (they report the live object, not a desired state).
func StartSubsystems(api *API, settings config.Settings, db *store.DB, credVault *vault.Vault, plane *ProxyPlane) *Subsystems {
	subsystems := &Subsystems{}
	runtime := NewRuntimeSettings(db, settings)
	registry := newAccountRegistry(db, settings)

	// OAuth import flows are request-driven, so they only need construction.
	api.Imports = oauthflow.NewService(oauthflow.ServiceOptions{
		DB:       db,
		Vault:    credVault,
		Settings: settings,
		Store:    oauthflow.NewStore(15 * time.Minute),
		Imports:  importer.New(db, credVault),
	})

	metricsCollector := metrics.NewCollector(metrics.CollectorOptions{
		DB:       db,
		Vault:    credVault,
		Settings: settings,
		Accounts: registry,
		Clients:  metrics.NewCreditClients(settings),
	})
	api.Metrics = metricsCollector
	if settings.MetricsEnabled {
		subsystems.MetricsScheduler = metrics.NewScheduler(metrics.SchedulerOptions{
			Collector: metricsCollector,
			Settings:  settings,
			Runtime:   runtime,
		})
		api.MetricsScheduler = subsystems.MetricsScheduler
		subsystems.MetricsScheduler.Start()
		slog.Info("metrics scheduler started", "interval_seconds", settings.MetricsIntervalSeconds)
	}

	checkinClient := checkin.NewClient(checkin.ClientOptions{
		BaseURL:      settings.CodeBuddyCheckinBase,
		StatusPath:   settings.CodeBuddyCheckinStatusPath,
		ClaimPath:    settings.CodeBuddyCheckinClaimPath,
		StatusMethod: settings.CodeBuddyCheckinStatusMethod,
		ClaimMethod:  settings.CodeBuddyCheckinClaimMethod,
		Timeout:      time.Duration(settings.CheckinTimeout) * time.Second,
	})
	checkinService := checkin.NewService(checkin.ServiceOptions{
		DB:       db,
		Vault:    credVault,
		Registry: registry,
		Settings: settings,
		Runtime:  runtime,
		Metrics:  metricsCollector,
		Client:   checkinClient,
	})
	api.Checkin = checkinService
	if settings.CheckinEnabled {
		subsystems.CheckinScheduler = checkin.NewScheduler(checkin.SchedulerOptions{
			Service:  checkinService,
			Runtime:  runtime,
			Settings: settings,
		})
		api.CheckinScheduler = subsystems.CheckinScheduler
		subsystems.CheckinScheduler.Start()
		slog.Info("checkin scheduler started", "at", settings.CheckinAt, "timezone", settings.CheckinTimezone)
	}

	growthAutomation := growth.NewAutomation(growth.AutomationOptions{
		DB:       db,
		Vault:    credVault,
		Settings: settings,
		Accounts: growthAccounts{registry: registry},
		Metrics:  metricsCollector,
		Runtime:  runtime,
	})
	api.Growth = growthAutomation
	if settings.GrowthSchedulerEnabled {
		subsystems.GrowthScheduler = growth.NewScheduler(growth.SchedulerOptions{
			Automation: growthAutomation,
			Settings:   settings,
			Runtime:    runtime,
		})
		subsystems.GrowthScheduler.Start()
		slog.Info("growth scheduler started", "interval_seconds", settings.GrowthSchedulerInterval)
	}

	// Usage rollup: recompute the minute/day/month buckets and prune expired
	// detail. This is the only writer of usage_rollups, so the console's trend
	// and the CSV export never race with it.
	rollupCtx, rollupCancel := context.WithCancel(context.Background())
	subsystems.RollupCancel = rollupCancel
	go runRollupLoop(rollupCtx, db, runtime, settings)

	// Credential rotation keeps short-lived bearer tokens alive; without it a
	// provider whose access token expires between scheduled runs would need a
	// manual re-login.
	if settings.CredentialRefresh {
		rotateCtx, rotateCancel := context.WithCancel(context.Background())
		subsystems.RotateCancel = rotateCancel
		go runCredentialRotation(rotateCtx, db, credVault, runtime, settings, plane)
	}

	return subsystems
}

// Stop halts every subsystem. It is idempotent.
func (s *Subsystems) Stop() {
	s.mu.Lock()
	if s.stopped {
		s.mu.Unlock()
		return
	}
	s.stopped = true
	s.mu.Unlock()
	if s.CheckinScheduler != nil {
		s.CheckinScheduler.Stop()
	}
	if s.GrowthScheduler != nil {
		s.GrowthScheduler.Stop()
	}
	if s.MetricsScheduler != nil {
		s.MetricsScheduler.Stop()
	}
	if s.RollupCancel != nil {
		s.RollupCancel()
	}
	if s.RotateCancel != nil {
		s.RotateCancel()
	}
}

// runRollupLoop recomputes usage buckets on the configured cadence.
func runRollupLoop(ctx context.Context, db *store.DB, runtime *RuntimeSettings, settings config.Settings) {
	interval := time.Duration(maxInt(runtime.UsageRollupIntervalSeconds(), 30)) * time.Second
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	// Run one round at startup so a restart immediately repairs any gap left by
	// downtime instead of waiting a full interval.
	rollupRound(ctx, db, runtime, settings)
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			rollupRound(ctx, db, runtime, settings)
		}
	}
}

func rollupRound(ctx context.Context, db *store.DB, runtime *RuntimeSettings, settings config.Settings) {
	roundCtx, cancel := context.WithTimeout(ctx, 2*time.Minute)
	defer cancel()
	groups, deleted, err := db.RollupOnce(roundCtx, time.Now(), runtime.UsageDetailRetentionDays())
	if err != nil {
		slog.Warn("usage rollup failed", "error", err)
		return
	}
	slog.Debug("usage rollup complete", "groups", groups, "deleted_events", deleted)
}

// runCredentialRotation refreshes bearer credentials before they expire.
//
// Rotation is proactive rather than reactive: the loop runs on its own cadence
// and only touches a credential whose expiry is inside the lead window, so a
// provider that cannot be refreshed is simply left alone until it truly expires.
func runCredentialRotation(ctx context.Context, db *store.DB, credVault *vault.Vault, runtime *RuntimeSettings, settings config.Settings, plane *ProxyPlane) {
	interval := time.Duration(maxInt(settings.CredentialRefreshEvery, 60)) * time.Second
	lead := time.Duration(maxInt(settings.CredentialRefreshLead, 60)) * time.Second
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			rotateOnce(ctx, db, credVault, lead, plane)
		}
	}
}

// accountRegistry implements the scheduler-facing account views over SQLite.
type accountRegistry struct {
	db       *store.DB
	settings config.Settings
}

func newAccountRegistry(db *store.DB, settings config.Settings) *accountRegistry {
	return &accountRegistry{db: db, settings: settings}
}

// EligibleForPurpose lists accounts whose purpose row is enabled and active.
func (r *accountRegistry) EligibleForPurpose(provider, purpose string) []checkin.AccountRef {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	accounts, err := r.db.ListAccounts(ctx, []string{provider})
	if err != nil {
		slog.Warn("list accounts failed", "provider", provider, "error", err)
		return nil
	}
	purposes, err := r.db.ListAllPurposes(ctx)
	if err != nil {
		slog.Warn("list purposes failed", "error", err)
		return nil
	}
	byAccount := map[string]store.Purpose{}
	for _, row := range purposes {
		if row.Purpose == purpose {
			byAccount[row.Provider+":"+row.AccountID] = row
		}
	}
	out := []checkin.AccountRef{}
	for _, account := range accounts {
		if !account.Enabled {
			continue
		}
		row, ok := byAccount[account.Provider+":"+account.AccountID]
		if !ok || !row.Enabled || row.Status != "active" {
			continue
		}
		out = append(out, checkin.AccountRef{
			Provider:           account.Provider,
			AccountID:          account.AccountID,
			Status:             row.Status,
			VerificationStatus: row.VerificationStatus,
			LastError:          row.LastError,
		})
	}
	return out
}

// Rebuild refreshes any in-memory account view. The Go build reads storage on
// demand, so there is nothing cached to invalidate; the method exists to satisfy
// the scheduler's interface.
func (r *accountRegistry) Rebuild(ctx context.Context) error { return nil }

// EligibleAccounts implements the metrics collector's account view.
func (r *accountRegistry) EligibleAccounts(provider string) []metrics.AccountRef {
	refs := r.EligibleForPurpose(provider, "chat")
	out := make([]metrics.AccountRef, 0, len(refs))
	for _, ref := range refs {
		out = append(out, metrics.AccountRef{Provider: ref.Provider, AccountID: ref.AccountID})
	}
	return out
}

// growthAccounts adapts the account registry to the growth package's own
// account view. The two interfaces are deliberately separate so neither
// scheduler depends on the other's types.
type growthAccounts struct{ registry *accountRegistry }

// EligibleForPurpose lists the accounts growth automation may act on.
func (g growthAccounts) EligibleForPurpose(provider, purpose string) []growth.AccountRef {
	refs := g.registry.EligibleForPurpose(provider, purpose)
	out := make([]growth.AccountRef, 0, len(refs))
	for _, ref := range refs {
		out = append(out, growth.AccountRef{
			Provider:           ref.Provider,
			AccountID:          ref.AccountID,
			Status:             ref.Status,
			VerificationStatus: ref.VerificationStatus,
			LastError:          ref.LastError,
		})
	}
	return out
}

// Rebuild satisfies the scheduler interface; the Go build reads storage on
// demand and caches nothing.
func (g growthAccounts) Rebuild(ctx context.Context) error { return nil }

var _ = models.ProviderWorkBuddy
