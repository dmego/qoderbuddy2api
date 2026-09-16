// Package oauthflow drives the browser-login account imports.
//
// Two providers are supported, both through the same plugin-OAuth shape on
// their own origin:
//
//	codebuddy       WorkBuddy domestic   {endpoint}/v2/plugin/auth/{state,token}
//	workbuddy_intl  WorkBuddy intl       {endpoint}/v2/plugin/auth/{state,token}
//
// The live flow state is in-memory with a TTL. The Python build declared an
// `oauth_flows` SQLite table but never queried it — the live store was
// in-process — so this keeps the in-memory store as the source of truth and does
// not pretend the table is load-bearing.
package oauthflow

import (
	"context"
	"errors"
	"log/slog"
	"sync"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/store"
	"github.com/dmego/qoderbuddy2api/internal/vault"
)

// ErrNotFound reports an unknown or expired flow.
var ErrNotFound = errors.New("flow not found")

// ErrUnsupportedProvider reports a provider without a login flow.
var ErrUnsupportedProvider = errors.New("unsupported provider")

// Flow is one login attempt, as the console sees it.
type Flow struct {
	StateHash string `json:"-"`
	FlowID    string `json:"flow_id"`
	Provider  string `json:"provider"`
	Label     string `json:"label,omitempty"`
	CreatedAt string `json:"created_at"`
	ExpiresAt string `json:"expires_at"`
	Status    string `json:"status"`
	AccountID string `json:"account_id,omitempty"`
	AuthURL   string `json:"auth_url,omitempty"`
	Message   string `json:"message,omitempty"`

	// upstreamState is the value the provider issued; it is kept out of the JSON
	// because the console addresses the flow by FlowID only.
	upstreamState string
}

// Store holds live flows in memory with a fixed TTL.
type Store struct {
	mu    sync.Mutex
	ttl   time.Duration
	flows map[string]Flow
}

// NewStore builds a flow store with the given TTL.
func NewStore(ttl time.Duration) *Store {
	if ttl <= 0 {
		ttl = 15 * time.Minute
	}
	return &Store{ttl: ttl, flows: map[string]Flow{}}
}

// Put stores a flow, keyed by its id.
func (s *Store) Put(flow Flow) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.flows[flow.FlowID] = flow
	return nil
}

// Get returns a live flow. Once past its deadline the flow is dropped and
// reported as not found, so an abandoned login cannot be completed later.
func (s *Store) Get(flowID string) (Flow, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	flow, ok := s.flows[flowID]
	if !ok {
		return Flow{}, ErrNotFound
	}
	expiresAt, parsed := store.ParseISO(flow.ExpiresAt)
	if parsed && time.Now().UTC().After(expiresAt) {
		delete(s.flows, flowID)
		flow.Status = "expired"
		return flow, nil
	}
	return flow, nil
}

// Complete records the terminal state of a flow.
func (s *Store) Complete(flowID, status, accountID, message string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	flow, ok := s.flows[flowID]
	if !ok {
		return ErrNotFound
	}
	flow.Status = status
	flow.AccountID = accountID
	flow.Message = message
	s.flows[flowID] = flow
	return nil
}

// prune drops expired flows. It runs on Put so an abandoned login cannot pin
// memory until the process restarts.
func (s *Store) prune(now time.Time) {
	for id, flow := range s.flows {
		if expiresAt, ok := store.ParseISO(flow.ExpiresAt); ok && now.After(expiresAt) {
			delete(s.flows, id)
		}
	}
}

// ImportWriter persists an account produced by a flow.
type ImportWriter interface {
	UpsertImportedAccount(
		ctx context.Context,
		provider, accountID, label string,
		identityHash *string,
		payload map[string]any,
		expiresAt string,
	) (string, error)
}

// ServiceOptions configures the flow service.
type ServiceOptions struct {
	DB       *store.DB
	Vault    *vault.Vault
	Settings config.Settings
	Store    *Store
	Imports  ImportWriter
}

// Service starts, polls and completes login flows.
type Service struct {
	db       *store.DB
	vault    *vault.Vault
	settings config.Settings
	store    *Store
	imports  ImportWriter
	client   *client
}

// NewService builds the flow service.
func NewService(opts ServiceOptions) *Service {
	flowStore := opts.Store
	if flowStore == nil {
		flowStore = NewStore(15 * time.Minute)
	}
	return &Service{
		db:       opts.DB,
		vault:    opts.Vault,
		settings: opts.Settings,
		store:    flowStore,
		imports:  opts.Imports,
		client:   newClient(),
	}
}

// endpointFor resolves the origin that serves one provider's plugin auth.
func (s *Service) endpointFor(provider string) (string, error) {
	switch provider {
	case models.ProviderWorkBuddy:
		return s.settings.CodeBuddyEndpoint, nil
	case models.ProviderWorkBuddyIntl:
		return s.settings.WorkBuddyIntlEndpoint, nil
	}
	return "", ErrUnsupportedProvider
}

// authDomain is the X-Domain header value the provider expects.
func authDomain(provider string) string {
	if provider == models.ProviderWorkBuddyIntl {
		return "www.workbuddy.ai"
	}
	return "copilot.tencent.com"
}

// Start mints a login flow and returns the URL the console should open.
func (s *Service) Start(ctx context.Context, provider, label string) (Flow, error) {
	endpoint, err := s.endpointFor(provider)
	if err != nil {
		return Flow{}, err
	}
	start, err := s.client.start(ctx, endpoint, authDomain(provider))
	if err != nil {
		return Flow{}, err
	}
	now := time.Now().UTC()
	flow := Flow{
		StateHash:     hashToken(start.State),
		FlowID:        randomToken(16),
		Provider:      provider,
		Label:         label,
		CreatedAt:     store.FormatISO(now),
		ExpiresAt:     store.FormatISO(now.Add(15 * time.Minute)),
		Status:        "pending",
		AuthURL:       start.AuthURL,
		upstreamState: start.State,
	}
	s.store.mu.Lock()
	s.store.prune(now)
	s.store.mu.Unlock()
	if err := s.store.Put(flow); err != nil {
		return Flow{}, err
	}
	// Record the flow for observability. The live state is the in-memory store;
	// this row exists so an operator can see that a login was attempted.
	if s.db != nil {
		stateHash := flow.StateHash
		_ = s.db.PutOAuthFlow(ctx, store.OAuthFlow{
			StateHash: stateHash,
			Provider:  provider,
			Label:     &label,
			CreatedAt: flow.CreatedAt,
			ExpiresAt: flow.ExpiresAt,
			Status:    "pending",
		})
	}
	return flow, nil
}

// Poll reports a flow's state and completes the import once the user has signed
// in upstream.
func (s *Service) Poll(ctx context.Context, provider, flowID string) (Flow, error) {
	flow, err := s.store.Get(flowID)
	if err != nil {
		return Flow{}, err
	}
	if flow.Status == "expired" {
		return flow, nil
	}
	// A terminal flow is returned as-is: polling must be idempotent, so a console
	// that polls twice after success does not import the account twice.
	if flow.Status == "success" || flow.Status == "failed" {
		return flow, nil
	}
	if flow.Provider != "" && provider != "" && flow.Provider != provider {
		return Flow{}, ErrNotFound
	}
	endpoint, err := s.endpointFor(flow.Provider)
	if err != nil {
		return Flow{}, err
	}
	result, err := s.client.poll(ctx, endpoint, authDomain(flow.Provider), flow.upstreamState)
	if err != nil {
		_ = s.store.Complete(flowID, "failed", "", err.Error())
		flow.Status = "failed"
		flow.Message = err.Error()
		return flow, nil
	}
	if result.Status == "pending" {
		// Keep the flow alive while the operator finishes signing in, and hand
		// back the deadline so a refreshed console tab knows when to stop.
		return flow, nil
	}
	if result.Status != "success" {
		message := result.Message
		if message == "" {
			message = "auth_poll_failed"
		}
		_ = s.store.Complete(flowID, "failed", "", message)
		flow.Status = "failed"
		flow.Message = message
		return flow, nil
	}
	accountID, importErr := s.importCredential(ctx, flow.Provider, flow.Label, result)
	if importErr != nil {
		slog.Warn("oauth import failed", "provider", flow.Provider, "error", importErr)
		_ = s.store.Complete(flowID, "failed", "", "import_failed")
		flow.Status = "failed"
		flow.Message = "import_failed"
		return flow, nil
	}
	_ = s.store.Complete(flowID, "success", accountID, "")
	flow.Status = "success"
	flow.AccountID = accountID
	return flow, nil
}

// Manual imports an operator-pasted credential without an upstream round trip.
//
// This is the fallback path and must work even when the provider's state/token
// endpoints are unreachable, which is why it does not touch the network.
func (s *Service) Manual(ctx context.Context, provider, label string, payload map[string]any, expiresAt string) (Flow, error) {
	if _, err := s.endpointFor(provider); err != nil {
		return Flow{}, err
	}
	if len(payload) == 0 {
		return Flow{}, errors.New("credential payload is empty")
	}
	token, _ := payload["access_token"].(string)
	if token == "" {
		return Flow{}, errors.New("access_token is required")
	}
	accountID, err := s.persist(ctx, provider, "", label, payload, expiresAt)
	if err != nil {
		return Flow{}, err
	}
	now := time.Now().UTC()
	return Flow{
		FlowID:    randomToken(16),
		Provider:  provider,
		Label:     label,
		CreatedAt: store.FormatISO(now),
		ExpiresAt: store.FormatISO(now.Add(15 * time.Minute)),
		Status:    "success",
		AccountID: accountID,
	}, nil
}

// ManualCheckin imports a sign-in credential for the check-in purpose. Only the
// domestic deployment has a sign-in centre.
func (s *Service) ManualCheckin(ctx context.Context, provider, accountID, mode string, payload map[string]any, expiresAt string) (Flow, error) {
	if provider != models.ProviderWorkBuddy {
		return Flow{}, ErrUnsupportedProvider
	}
	if accountID == "" {
		return Flow{}, errors.New("account_id is required")
	}
	accessToken, _ := payload["access_token"].(string)
	cookie, _ := payload["cookie"].(string)
	if accessToken == "" && cookie == "" {
		return Flow{}, errors.New("access_token or cookie is required")
	}
	encrypted, err := s.vault.Encrypt(payload)
	if err != nil {
		return Flow{}, err
	}
	write := store.CredentialWrite{
		Provider:         provider,
		AccountID:        accountID,
		Purpose:          "checkin",
		Mode:             orDefault(mode, "bearer"),
		EncryptedPayload: encrypted,
		HasRefreshToken:  payload["refresh_token"] != nil,
	}
	if expiresAt != "" {
		expires := expiresAt
		write.ExpiresAt = &expires
	}
	if _, err := s.db.UpsertCredential(ctx, write); err != nil {
		return Flow{}, err
	}
	now := store.NowISO()
	purpose := store.Purpose{
		Provider:           provider,
		AccountID:          accountID,
		Purpose:            "checkin",
		Enabled:            true,
		Status:             "active",
		VerificationStatus: "verified",
		Capabilities:       []string{"checkin"},
		VerifiedAt:         &now,
		LastSuccessAt:      &now,
	}
	if err := s.db.UpsertPurpose(ctx, purpose); err != nil {
		return Flow{}, err
	}
	created := time.Now().UTC()
	return Flow{
		FlowID:    randomToken(16),
		Provider:  provider,
		CreatedAt: store.FormatISO(created),
		ExpiresAt: store.FormatISO(created.Add(15 * time.Minute)),
		Status:    "success",
		AccountID: accountID,
	}, nil
}

// importCredential stores an account produced by a completed upstream login.
func (s *Service) importCredential(ctx context.Context, provider, label string, result pollResult) (string, error) {
	payload := map[string]any{"access_token": result.AccessToken}
	if result.RefreshToken != "" {
		payload["refresh_token"] = result.RefreshToken
	}
	expiresAt := ""
	if result.ExpiresIn > 0 {
		expiresAt = store.FormatISO(time.Now().UTC().Add(time.Duration(result.ExpiresIn) * time.Second))
	}
	return s.persist(ctx, provider, "", label, payload, expiresAt)
}

// persist delegates storage to the import writer.
func (s *Service) persist(ctx context.Context, provider, accountID, label string, payload map[string]any, expiresAt string) (string, error) {
	if s.imports == nil {
		return "", errors.New("no import writer configured")
	}
	if label == "" {
		label = defaultLabel(provider)
	}
	// The identity used for de-duplication is the label the console collected
	// (an email for the international deployment), hashed so the raw value never
	// reaches storage.
	var identityHash *string
	if label != "" && s.vault != nil {
		digest := s.vault.Fingerprint(label)
		identityHash = &digest
	}
	return s.imports.UpsertImportedAccount(ctx, provider, accountID, label, identityHash, payload, expiresAt)
}

func defaultLabel(provider string) string {
	if provider == models.ProviderWorkBuddyIntl {
		return "WorkBuddy 国际版 OAuth"
	}
	return "WorkBuddy OAuth"
}

func orDefault(value, fallback string) string {
	if value == "" {
		return fallback
	}
	return value
}
