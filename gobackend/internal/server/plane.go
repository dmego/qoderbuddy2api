package server

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"log/slog"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/dmego/qoderbuddy2api/gobackend/internal/chatwire"
	"github.com/dmego/qoderbuddy2api/gobackend/internal/config"
	"github.com/dmego/qoderbuddy2api/gobackend/internal/models"
	"github.com/dmego/qoderbuddy2api/gobackend/internal/providers"
	"github.com/dmego/qoderbuddy2api/gobackend/internal/store"
	"github.com/dmego/qoderbuddy2api/gobackend/internal/vault"
)

// ProxyPlane owns the provider pools, the unified catalog and the router.
//
// It deliberately has no reference to the database, the credential vault or the
// admin key: everything the request path needs is resolved at snapshot-build
// time. That keeps the "the request path cannot read storage secrets" property
// of the original two-process split without paying for a second process.
type ProxyPlane struct {
	settings config.Settings
	vault    *vault.Vault

	mu       sync.RWMutex
	pools    map[string]*providers.Pool
	router   *providers.Router
	catalog  map[string]models.Unified
	defs     map[string][]models.Definition
	version  int64
	keyHash  map[string]bool // sha256 hex of every accepted proxy key
	keyCount int
	inFlight atomic.Int64
}

// NewProxyPlane builds an empty plane bound to one credential vault.
func NewProxyPlane(settings config.Settings, credVault *vault.Vault) *ProxyPlane {
	pools := map[string]*providers.Pool{
		models.ProviderWorkBuddy:     providers.NewPool(models.ProviderWorkBuddy),
		models.ProviderWorkBuddyIntl: providers.NewPool(models.ProviderWorkBuddyIntl),
	}
	plane := &ProxyPlane{
		settings: settings,
		vault:    credVault,
		pools:    pools,
		keyHash:  map[string]bool{},
	}
	plane.defs = models.LoadDefinitions(settings.ModelConfigPath)
	plane.catalog = models.BuildCatalog(plane.defs, models.LoadUnifiedOverrides(settings.ModelConfigPath))
	plane.router = providers.NewRouter(plane.catalog, pools, nil)
	return plane
}

// Catalog returns the current unified catalog (a copy of the map header).
func (p *ProxyPlane) Catalog() map[string]models.Unified {
	p.mu.RLock()
	defer p.mu.RUnlock()
	out := make(map[string]models.Unified, len(p.catalog))
	for key, value := range p.catalog {
		out[key] = value
	}
	return out
}

// Definitions returns the per-provider model definitions.
func (p *ProxyPlane) Definitions() map[string][]models.Definition {
	p.mu.RLock()
	defer p.mu.RUnlock()
	out := make(map[string][]models.Definition, len(p.defs))
	for key, value := range p.defs {
		out[key] = append([]models.Definition(nil), value...)
	}
	return out
}

// AvailableModels lists models with at least one live route.
func (p *ProxyPlane) AvailableModels() []models.Unified {
	p.mu.RLock()
	router := p.router
	p.mu.RUnlock()
	if router == nil {
		return nil
	}
	return router.AvailableModels()
}

// Version reports the current snapshot generation.
func (p *ProxyPlane) Version() int64 {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.version
}

// Resolve maps a requested model id (canonical, prefixed, or upstream) to a
// canonical id, or reports an unknown model.
func (p *ProxyPlane) Resolve(model string) (models.Unified, error) {
	p.mu.RLock()
	catalog := p.catalog
	router := p.router
	p.mu.RUnlock()

	if index := strings.Index(model, "/"); index >= 0 {
		provider, upstream := model[:index], model[index+1:]
		for _, entry := range catalog {
			for _, route := range entry.Routes {
				if route.Provider == provider && route.UpstreamID == upstream {
					return entry, nil
				}
			}
		}
		return models.Unified{}, &UnknownModelError{Model: model, Available: availableIDs(catalog)}
	}
	if entry, ok := catalog[model]; ok {
		return entry, nil
	}
	// A bare upstream id (e.g. "DeepSeek-V4.1-Flash") still resolves.
	for _, entry := range catalog {
		for _, route := range entry.Routes {
			if route.UpstreamID == model {
				return entry, nil
			}
		}
	}
	_ = router
	return models.Unified{}, &UnknownModelError{Model: model, Available: availableIDs(catalog)}
}

// UnknownModelError reports a model id the catalog does not know.
type UnknownModelError struct {
	Model     string
	Available []string
}

func (e *UnknownModelError) Error() string {
	preview := e.Available
	if len(preview) > 10 {
		preview = preview[:10]
	}
	return "Unknown model: " + e.Model + ". Available: " + strings.Join(preview, ", ") + "..."
}

func availableIDs(catalog map[string]models.Unified) []string {
	out := make([]string, 0, len(catalog))
	for id := range catalog {
		out = append(out, id)
	}
	sort.Strings(out)
	return out
}

// Complete runs a non-streaming completion.
func (p *ProxyPlane) Complete(ctx context.Context, request *chatwire.ChatRequest) (map[string]any, error) {
	p.inFlight.Add(1)
	defer p.inFlight.Add(-1)
	p.mu.RLock()
	router := p.router
	p.mu.RUnlock()
	if router == nil {
		return nil, errors.New("proxy plane not ready")
	}
	return router.Complete(ctx, request)
}

// Stream opens a streaming completion.
func (p *ProxyPlane) Stream(ctx context.Context, request *chatwire.ChatRequest) (providers.FrameStream, error) {
	p.mu.RLock()
	router := p.router
	p.mu.RUnlock()
	if router == nil {
		return nil, errors.New("proxy plane not ready")
	}
	return router.Stream(ctx, request)
}

// SlotCount reports the total number of loaded provider credentials.
func (p *ProxyPlane) SlotCount() int {
	p.mu.RLock()
	defer p.mu.RUnlock()
	total := 0
	for _, pool := range p.pools {
		total += pool.SlotCount()
	}
	return total
}

// InFlight reports how many proxied requests are currently being served.
func (p *ProxyPlane) InFlight() int {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return int(p.inFlight.Load())
}

// QuotaBlocks aggregates the active model-scoped blocks across pools.
func (p *ProxyPlane) QuotaBlocks() []providers.QuotaBlock {
	p.mu.RLock()
	pools := make([]*providers.Pool, 0, len(p.pools))
	for _, pool := range p.pools {
		pools = append(pools, pool)
	}
	p.mu.RUnlock()
	var out []providers.QuotaBlock
	for _, pool := range pools {
		out = append(out, pool.QuotaBlocks()...)
	}
	return out
}

// AcceptProxyKey reports whether the presented bearer token is an accepted
// proxy key. When no key is configured the proxy is open, matching the legacy
// "unset proxy key = open proxy" behaviour.
func (p *ProxyPlane) AcceptProxyKey(token string) bool {
	p.mu.RLock()
	required := len(p.keyHash) > 0
	accepted := p.keyHash[hashToken(token)]
	p.mu.RUnlock()
	if !required {
		return true
	}
	return accepted
}

// ProxyAuthRequired reports whether any proxy key is configured.
func (p *ProxyPlane) ProxyAuthRequired() bool {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return len(p.keyHash) > 0
}

// Reload rebuilds the credential slots, catalog and router from storage.
//
// This replaces the Python Control→Worker snapshot handshake. Credentials are
// decrypted here, once per rebuild, and only the resulting bearer strings reach
// the provider pools.
func (p *ProxyPlane) Reload(ctx context.Context, db *store.DB) error {
	accounts, err := db.ListAccounts(ctx, models.KnownProviders)
	if err != nil {
		return err
	}
	purposes, err := db.ListAllPurposes(ctx)
	if err != nil {
		return err
	}
	active := map[string]bool{}
	for _, purpose := range purposes {
		if purpose.Purpose == "chat" && purpose.Enabled && purpose.Status == "active" {
			active[purpose.Provider+":"+purpose.AccountID] = true
		}
	}

	slots := map[string]map[string]providers.Provider{}
	for _, provider := range models.KnownProviders {
		slots[provider] = map[string]providers.Provider{}
	}
	for _, account := range accounts {
		if !account.Enabled {
			continue
		}
		if !models.ChatOnlyProviders[account.Provider] && !active[account.Provider+":"+account.AccountID] {
			// Accounts whose chat purpose is missing or disabled are skipped,
			// matching registry.snapshot("chat").
			continue
		}
		record, err := db.GetCredential(ctx, account.Provider, account.AccountID, "chat")
		if err != nil {
			slog.Warn("snapshot skipped account without chat credential",
				"provider", account.Provider, "account", account.AccountID)
			continue
		}
		payload, err := p.vault.Decrypt(record.EncryptedPayload)
		if err != nil {
			slog.Warn("snapshot skipped undecryptable credential",
				"provider", account.Provider, "account", account.AccountID, "error", err)
			continue
		}
		token := primaryToken(account.Provider, payload)
		if token == "" {
			continue
		}
		key := account.Provider + ":" + account.AccountID
		switch account.Provider {
		case models.ProviderWorkBuddy:
			slots[account.Provider][key] = providers.NewWorkBuddy(token, p.settings.CodeBuddyEndpoint, p.settings.CodeBuddyDefaultReasoning)
		case models.ProviderWorkBuddyIntl:
			slots[account.Provider][key] = providers.NewWorkBuddyIntl(token, p.settings.WorkBuddyIntlEndpoint, p.settings.WorkBuddyIntlReasoning)
		}
	}
	// Legacy env-provided tokens stay supported for a bare .env deployment.
	for index, token := range p.settings.LegacyCodeBuddyTokens {
		slots[models.ProviderWorkBuddy]["codebuddy:cb-env-"+itoa(index)] =
			providers.NewWorkBuddy(token, p.settings.CodeBuddyEndpoint, p.settings.CodeBuddyDefaultReasoning)
	}
	for index, token := range p.settings.LegacyIntlTokens {
		slots[models.ProviderWorkBuddyIntl]["workbuddy_intl:wbintl-env-"+itoa(index)] =
			providers.NewWorkBuddyIntl(token, p.settings.WorkBuddyIntlEndpoint, p.settings.WorkBuddyIntlReasoning)
	}

	// Catalog enablement from the admin model page.
	defs := models.LoadDefinitions(p.settings.ModelConfigPath)
	for provider, definitions := range defs {
		enabled, err := db.EnabledCatalogModels(ctx, provider)
		if err != nil || len(enabled) == 0 {
			continue
		}
		filtered := make([]models.Definition, 0, len(definitions))
		for _, definition := range definitions {
			if value, known := enabled[definition.ID]; known && !value {
				continue
			}
			filtered = append(filtered, definition)
		}
		defs[provider] = filtered
	}
	catalog := models.BuildCatalog(defs, models.LoadUnifiedOverrides(p.settings.ModelConfigPath))

	policyRows, err := db.ListRoutePolicies(ctx)
	if err != nil {
		return err
	}
	policies := map[string][]providers.RoutePolicy{}
	for _, row := range policyRows {
		policies[row.ModelID] = append(policies[row.ModelID], providers.RoutePolicy{
			Provider: row.Provider,
			Priority: row.Priority,
			Weight:   row.Weight,
			Enabled:  row.Enabled,
		})
	}
	blocks, err := db.ListAccountModelBlocks(ctx)
	if err != nil {
		return err
	}
	hardBlocks := map[string]map[[2]string]string{}
	for _, block := range blocks {
		if block.Source != "manual" {
			continue
		}
		if hardBlocks[block.Provider] == nil {
			hardBlocks[block.Provider] = map[[2]string]string{}
		}
		hardBlocks[block.Provider][[2]string{block.Provider + ":" + block.AccountID, block.ModelID}] = block.Reason
	}

	keyHashes := map[string]bool{}
	if p.settings.ProxyAPIKey != "" {
		keyHashes[hashToken(p.settings.ProxyAPIKey)] = true
	}
	stored, err := db.ActiveProxyKeyHashes(ctx, time.Now())
	if err != nil {
		return err
	}
	for _, hash := range stored {
		keyHashes[hash] = true
	}

	p.mu.Lock()
	for provider, pool := range p.pools {
		pool.UpdateSlots(slots[provider])
		if hard, ok := hardBlocks[provider]; ok {
			pool.SetHardBlocks(hard)
		} else {
			pool.SetHardBlocks(nil)
		}
	}
	p.defs = defs
	p.catalog = catalog
	p.router = providers.NewRouter(catalog, p.pools, policies)
	p.version++
	p.keyHash = keyHashes
	p.keyCount = len(keyHashes)
	p.mu.Unlock()
	return nil
}

// Close releases every provider connection.
func (p *ProxyPlane) Close() {
	p.mu.Lock()
	pools := p.pools
	p.mu.Unlock()
	for _, pool := range pools {
		pool.UpdateSlots(nil)
	}
}

// primaryToken extracts the bearer value from a decrypted credential payload.
func primaryToken(provider string, payload map[string]any) string {
	for _, key := range []string{"access_token", "token"} {
		if value, ok := payload[key].(string); ok && value != "" {
			return value
		}
	}
	return ""
}

// hashToken renders the sha256 hex digest used for key comparison, matching
// admin/crypto.hash_token.
func hashToken(token string) string {
	digest := sha256.Sum256([]byte(token))
	return hex.EncodeToString(digest[:])
}

func itoa(value int) string {
	if value == 0 {
		return "0"
	}
	negative := value < 0
	if negative {
		value = -value
	}
	var buffer [20]byte
	index := len(buffer)
	for value > 0 {
		index--
		buffer[index] = byte('0' + value%10)
		value /= 10
	}
	if negative {
		index--
		buffer[index] = '-'
	}
	return string(buffer[index:])
}
