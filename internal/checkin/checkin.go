package checkin

import (
	"context"

	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/store"
	"github.com/dmego/qoderbuddy2api/internal/vault"
)

// AccountRef identifies one eligible account.
type AccountRef struct {
	Provider           string
	AccountID          string
	Status             string
	VerificationStatus string
	LastError          *string
}

// AccountLister yields the accounts eligible for a purpose.
type AccountLister interface {
	EligibleForPurpose(provider, purpose string) []AccountRef
	Rebuild(ctx context.Context) error
}

// RuntimeSettings supplies live overrides written through the settings API.
type RuntimeSettings interface {
	CheckinEnabled() bool
	CheckinAt() string
	CheckinTimezone() string
	CheckinCatchUp() bool
	CheckinCatchUpWindowHours() int
	CheckinJitterMinSeconds() int
	CheckinJitterMaxSeconds() int
	CheckinRetryLimit() int
}

// QuotaRefresher re-reads quota packages for one account after a claim.
type QuotaRefresher interface {
	RefreshOne(ctx context.Context, provider, accountID string) (map[string]any, error)
}

// ServiceOptions configures the batch service.
type ServiceOptions struct {
	DB       *store.DB
	Vault    *vault.Vault
	Registry AccountLister
	Settings config.Settings
	Runtime  RuntimeSettings
	Metrics  QuotaRefresher
	Client   *Client
}

// RunSummary is the outcome of one batch.
type RunSummary struct {
	RunID   string
	Status  string
	Results []Result
}

// SchedulerOptions configures the daily scheduler.
type SchedulerOptions struct {
	Service  *Service
	Runtime  RuntimeSettings
	Settings config.Settings
}
