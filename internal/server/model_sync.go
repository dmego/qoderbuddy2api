package server

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/chatwire"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/providers"
	"github.com/dmego/qoderbuddy2api/internal/store"
)

// catalogSourceDiscovery marks a catalog row that a sync discovered by probing
// upstream rather than reading config/models.json. The plane merges those rows
// into the routable definitions, so the source has to be distinguishable from
// the "definition" rows the config produces.
const catalogSourceDiscovery = "discovery"

// modelProbeConcurrency bounds the parallel upstream probes a sync issues. The
// gateway starts rate limiting above a handful of simultaneous streams.
const modelProbeConcurrency = 5

// candidateModelIDs are the ids a sync probes in addition to whatever the
// config already lists.
//
// WorkBuddy publishes no model-catalog endpoint, so the only way to learn about
// a model is to ask for it: /v2/chat/completions answers 200 for a real id and
// 400 code=11102 for an unknown one. That makes discovery a list-maintenance
// problem, and this list is the one place a newly announced model is added.
// Ids are lowercase — upstream is case sensitive and "Space-Bunny" is rejected
// while "space-bunny" is served.
var candidateModelIDs = []string{
	"space-bunny",
	"glm-5.4", "glm-5.4-flash", "glm-5v",
	"deepseek-v3.1", "deepseek-v4.1",
	"kimi-k2.8", "kimi-k2.9", "kimi-k3",
	"minimax-m2.5", "minimax-m4",
	"hy4", "hy4-flash",
}

// syncReport is the outcome of one provider sync, shaped the way the console
// reads it.
type syncReport struct {
	Status  string `json:"status"`
	Added   int    `json:"added"`
	Updated int    `json:"updated"`
	Removed int    `json:"removed"`
	Probed  int    `json:"probed"`
}

// handleSyncModels is the full upstream sync: every discoverable provider, each
// isolated so one failure does not abort the other.
func (a *API) handleSyncModels(w http.ResponseWriter, r *http.Request) {
	if a.Plane == nil {
		writeError(w, http.StatusServiceUnavailable, "runtime_starting")
		return
	}
	entries := map[string]any{}
	totals := map[string]int{"added": 0, "updated": 0, "removed": 0}
	for _, provider := range discoverableProviders() {
		report, err := a.syncProviderModels(r.Context(), provider)
		if err != nil {
			entries[provider] = map[string]any{"status": "failed", "error": syncErrorName(err)}
			continue
		}
		entries[provider] = map[string]any{
			"status": report.Status, "added": report.Added,
			"updated": report.Updated, "removed": report.Removed, "probed": report.Probed,
		}
		totals["added"] += report.Added
		totals["updated"] += report.Updated
		totals["removed"] += report.Removed
	}
	a.audit(r, "model.sync", "catalog", "", map[string]any{"providers": entries})
	if err := a.refreshPlane(r); err != nil {
		writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status": "succeeded", "providers": entries,
		"added": totals["added"], "updated": totals["updated"], "removed": totals["removed"],
	})
}

// syncProviderHandler binds the per-provider sync route to one provider. The
// route is registered per provider rather than with a {provider} wildcard so it
// cannot be confused with "/{modelID}/probe" (see admin_routes.go).
func (a *API) syncProviderHandler(provider string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if a.Plane == nil {
			writeError(w, http.StatusServiceUnavailable, "runtime_starting")
			return
		}
		report, err := a.syncProviderModels(r.Context(), provider)
		if err != nil {
			writeError(w, http.StatusBadGateway, syncErrorName(err))
			return
		}
		a.audit(r, "model.sync", "catalog", provider, map[string]any{
			"added": report.Added, "updated": report.Updated, "removed": report.Removed,
		})
		if err := a.refreshPlane(r); err != nil {
			writeError(w, http.StatusServiceUnavailable, "provider_pool_refresh_failed")
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"status": report.Status, "added": report.Added, "updated": report.Updated,
			"removed": report.Removed, "probed": report.Probed,
		})
	}
}

// discoverableProviders lists the providers a sync may probe.
func discoverableProviders() []string {
	return []string{models.ProviderWorkBuddy, models.ProviderWorkBuddyIntl}
}

// syncProviderModels probes the candidate ids and reconciles the catalog.
//
// Reconciliation is add/update only: a model upstream no longer serves is left
// in place rather than removed. A probe failure and a deleted model look alike
// from here, and silently dropping a routable model because one probe timed out
// is the more expensive mistake.
func (a *API) syncProviderModels(ctx context.Context, provider string) (syncReport, error) {
	candidates := a.syncCandidates(provider)
	if len(candidates) == 0 {
		return syncReport{Status: "succeeded"}, nil
	}
	return a.reconcileModels(ctx, provider, a.probeModels(ctx, provider, candidates))
}

// reconcileModels folds probe outcomes into the catalog.
//
// Reconciliation is add/update only: a model upstream no longer serves is left
// in place rather than removed. A probe failure and a deleted model look alike
// from here, and silently dropping a routable model because one probe timed out
// is the more expensive mistake.
func (a *API) reconcileModels(ctx context.Context, provider string, outcomes []probeOutcome) (syncReport, error) {
	existing, err := a.DB.ListCatalogModels(ctx, []string{provider})
	if err != nil {
		return syncReport{}, err
	}
	known := map[string]store.CatalogModel{}
	for _, row := range existing {
		known[row.ModelID] = row
	}
	configured := map[string]models.Definition{}
	for _, definition := range models.LoadDefinitions(a.Settings.ModelConfigPath)[provider] {
		configured[definition.ID] = definition
	}

	report := syncReport{Status: "succeeded", Probed: len(outcomes)}
	for _, outcome := range outcomes {
		if !outcome.Exists {
			continue
		}
		prior, found := known[outcome.ModelID]
		if found && prior.Source != catalogSourceDiscovery {
			// A row owned by the config is left to the config; discovery only
			// records what it found. Rewriting it would let a truncated probe
			// strip a capability the config asserts.
			report.Updated++
			continue
		}
		capabilities := outcome.Capabilities
		if found {
			// Discovery never downgrades a capability it previously recorded.
			// Reasoning detection is stochastic — space-bunny emitted reasoning
			// on 2 of 3 trivial prompts and glm-5.3-flash on 1 of 4 reasoning
			// prompts — so a miss is not evidence the capability is gone. Losing
			// it would need a positive signal, which the probe cannot produce.
			capabilities = keepDiscoveredCapabilities(prior.Capabilities, capabilities)
		}
		if found && equalCapabilities(prior.Capabilities, capabilities) && prior.Enabled {
			report.Updated++
			continue
		}
		row := store.CatalogModel{
			Provider:     provider,
			ModelID:      outcome.ModelID,
			DisplayName:  displayNameFor(outcome.ModelID, prior, configured[outcome.ModelID]),
			Capabilities: capabilities,
			Source:       catalogSourceDiscovery,
			Enabled:      true,
		}
		if found && !prior.Enabled {
			// A model the operator disabled must not come back on a sync.
			row.Enabled = false
		}
		if err := a.DB.UpsertCatalogModel(ctx, row); err != nil {
			return syncReport{}, err
		}
		if found {
			report.Updated++
		} else {
			report.Added++
		}
	}
	return report, nil
}

// keepDiscoveredCapabilities unions the capabilities a previous discovery
// recorded into the freshly probed set, so a stochastic probe miss cannot strip
// a capability the catalog already advertises.
func keepDiscoveredCapabilities(prior, probed []string) []string {
	merged := append([]string(nil), probed...)
	present := map[string]bool{}
	for _, name := range probed {
		present[name] = true
	}
	for _, name := range prior {
		if present[name] {
			continue
		}
		switch name {
		case "reasoning", "reasoning_effort", "tool_calling":
			// Only additive capabilities are preserved: they are the ones the
			// probe can under-report.
			merged = append(merged, name)
		}
	}
	return merged
}

// syncCandidates lists the ids to probe for one provider: what the config
// already defines, plus the discovery candidates, deduplicated and sorted so the
// reconciliation order is stable.
func (a *API) syncCandidates(provider string) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, definition := range models.LoadDefinitions(a.Settings.ModelConfigPath)[provider] {
		if definition.ID == "" || seen[definition.ID] {
			continue
		}
		seen[definition.ID] = true
		out = append(out, definition.ID)
	}
	for _, candidate := range candidateModelIDs {
		if seen[candidate] {
			continue
		}
		seen[candidate] = true
		out = append(out, candidate)
	}
	sort.Strings(out)
	return out
}

// probeOutcome is one model's probe result.
type probeOutcome struct {
	ModelID      string
	Exists       bool
	Capabilities []string
}

// probeModels probes every candidate concurrently and returns the outcomes in
// the input order, so reconciliation is deterministic.
func (a *API) probeModels(ctx context.Context, provider string, candidates []string) []probeOutcome {
	outcomes := make([]probeOutcome, len(candidates))
	semaphore := make(chan struct{}, modelProbeConcurrency)
	var wait sync.WaitGroup
	for index, candidate := range candidates {
		wait.Add(1)
		go func(index int, candidate string) {
			defer wait.Done()
			semaphore <- struct{}{}
			defer func() { <-semaphore }()
			outcomes[index] = a.probeModel(ctx, provider, candidate)
		}(index, candidate)
	}
	wait.Wait()
	return outcomes
}

// probePrompt is the question the reasoning probe asks.
//
// A trivial prompt ("hi") makes reasoning detection a coin flip: measured
// 2026-10-06, space-bunny emitted reasoning on 2 of 3 attempts for "hi" but 4 of
// 4 for this multi-step question. A model that answers "hi" in one greeting
// token may simply not think, which is indistinguishable from a model that
// cannot think at all — and the catalog would then advertise the wrong
// capability.
const probePrompt = "一个农夫有17只羊，除了9只以外都死了，还剩几只？请一步步推理。"

// probeModel asks upstream whether one model id exists and whether it reasons.
//
// A 200 means the id is servable; reasoning is then decided by a two-pass probe.
// The passes matter: some models only think when reasoning_effort is set
// (deepseek-v4.1-flash emits nothing at effort=none), while others only think
// when it is NOT set (space-bunny ignores effort=low but emits reasoning
// without it). A single pass therefore misclassifies one family or the other.
//
// Any failure — 400/11102 for an unknown id or a transport fault — is reported
// as "does not exist", which is why the caller never removes rows on a miss.
func (a *API) probeModel(ctx context.Context, provider, modelID string) probeOutcome {
	outcome := probeOutcome{ModelID: modelID}
	probeCtx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	base := func() *chatwire.ChatRequest {
		return &chatwire.ChatRequest{
			Model:     modelID,
			Stream:    true,
			MaxTokens: new(1024),
			Messages: []chatwire.Message{
				{Role: "system", Content: json.RawMessage(`"You are a helpful assistant."`)},
				{Role: "user", Content: mustMarshalJSON(probePrompt)},
			},
		}
	}
	withEffort := base()
	withEffort.ReasoningEffort = "low"
	reasoning, err := a.streamProbe(probeCtx, provider, withEffort)
	if err != nil {
		slog.Debug("model probe did not confirm a model", "provider", provider, "model", modelID, "error", err)
		return outcome
	}
	if !reasoning {
		// The bare pass exists for the family that rejects reasoning_effort. Its
		// failure is not fatal: the first pass already proved the model exists.
		if bare, bareErr := a.streamProbe(probeCtx, provider, base()); bareErr == nil {
			reasoning = bare
		}
	}
	outcome.Exists = true
	outcome.Capabilities = []string{"chat", "streaming"}
	if reasoning {
		outcome.Capabilities = append(outcome.Capabilities, "reasoning", "reasoning_effort")
	}
	return outcome
}

// mustMarshalJSON encodes a string as a JSON literal for a message body. The
// input is a compile-time constant, so a failure is impossible in practice.
func mustMarshalJSON(value string) json.RawMessage {
	encoded, err := json.Marshal(value)
	if err != nil {
		return json.RawMessage(`""`)
	}
	return encoded
}

// streamProbe runs one probe stream against a specific provider's pool and
// reports whether the model emitted reasoning content.
//
// It bypasses the router on purpose: discovery probes ids that are not in the
// catalog yet, so the router has no route to resolve them. Talking to the pool
// directly also keeps a discovery probe from disturbing route health.
func (a *API) streamProbe(ctx context.Context, provider string, request *chatwire.ChatRequest) (bool, error) {
	a.Plane.mu.RLock()
	pool := a.Plane.pools[provider]
	a.Plane.mu.RUnlock()
	if pool == nil || !pool.HasAvailableSlots() {
		return false, errors.New("no credential available for " + provider)
	}
	stream, err := pool.Stream(ctx, request)
	if err != nil {
		return false, err
	}
	defer stream.Close()
	reasoning := false
	for {
		frame, err := stream.Next()
		if err != nil {
			// The probe reads to the end rather than stopping at the first
			// content frame: some models emit reasoning after a short content
			// burst, so stopping early would under-report the capability.
			return reasoning, nil
		}
		if providers.FrameHasReasoning(frame) {
			reasoning = true
		}
	}
}

// equalCapabilities compares two capability lists irrespective of order.
func equalCapabilities(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	index := map[string]bool{}
	for _, item := range left {
		index[item] = true
	}
	for _, item := range right {
		if !index[item] {
			return false
		}
	}
	return true
}

// displayNameFor keeps an existing name, then the config's, then the raw id.
func displayNameFor(modelID string, prior store.CatalogModel, configured models.Definition) string {
	if prior.DisplayName != "" {
		return prior.DisplayName
	}
	if configured.Name != "" {
		return configured.Name
	}
	return modelID
}

// capabilityFlags turns a stored capability list into the flags the catalog
// builder expects.
func capabilityFlags(names []string) models.Capabilities {
	var flags models.Capabilities
	for _, name := range names {
		switch strings.ToLower(name) {
		case "chat":
			flags.Chat = true
		case "streaming":
			flags.Streaming = true
		case "tool_calling":
			flags.ToolCalling = true
		case "reasoning":
			flags.Reasoning = true
		case "reasoning_effort":
			flags.ReasoningEffort = true
		}
	}
	return flags
}

// appendDefinitionOnce adds a definition unless the id is already present.
func appendDefinitionOnce(definitions []models.Definition, definition models.Definition) []models.Definition {
	for _, existing := range definitions {
		if existing.ID == definition.ID {
			return definitions
		}
	}
	return append(definitions, definition)
}

// syncErrorName renders a sync failure as a bounded label the console displays.
func syncErrorName(err error) string {
	if err == nil {
		return ""
	}
	var upstream *providers.UpstreamError
	if errors.As(err, &upstream) {
		return "upstream_" + itoa(upstream.StatusCode)
	}
	return truncate(err.Error(), 120)
}
