package server

import (
	"context"
	"net/http"
	"testing"

	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/store"
)

// TestSyncRoutesAreRegistered is the regression for the "从上游同步" button that
// answered 405/404: the console POSTs /models/sync and /models/sync/{provider},
// and neither route existed in the Go build, so the action could never work.
func TestSyncRoutesAreRegistered(t *testing.T) {
	api, _ := newTestAPI(t)
	for _, target := range []string{
		"/api/admin/models/sync",
		"/api/admin/models/sync/codebuddy",
		"/api/admin/models/sync/workbuddy_intl",
	} {
		recorder := adminRequest(t, api, http.MethodPost, target, nil)
		if recorder.Code == http.StatusNotFound || recorder.Code == http.StatusMethodNotAllowed {
			t.Fatalf("POST %s = %d, want a registered route", target, recorder.Code)
		}
	}
	// An unknown provider has no route in this build (qoder was dropped with the
	// rewrite), so a 404 is the honest answer rather than a fabricated 400.
	recorder := adminRequest(t, api, http.MethodPost, "/api/admin/models/sync/qoder", nil)
	if recorder.Code != http.StatusNotFound {
		t.Fatalf("POST sync/qoder = %d, want 404 for a provider this build does not have", recorder.Code)
	}
}

// TestSyncCandidatesIncludeConfigAndDiscovery pins the probe list: everything
// the config defines must be re-probed (so a capability upgrade is noticed) and
// the discovery list must be unioned in without duplicates.
func TestSyncCandidatesIncludeConfigAndDiscovery(t *testing.T) {
	api, _ := newTestAPI(t)
	candidates := api.syncCandidates(models.ProviderWorkBuddy)

	seen := map[string]bool{}
	for _, candidate := range candidates {
		if seen[candidate] {
			t.Fatalf("candidate %q listed twice", candidate)
		}
		seen[candidate] = true
	}
	for _, definition := range models.LoadDefinitions(api.Settings.ModelConfigPath)[models.ProviderWorkBuddy] {
		if !seen[definition.ID] {
			t.Fatalf("configured model %q missing from the probe list", definition.ID)
		}
	}
	if !seen["space-bunny"] {
		t.Fatal("space-bunny must be probed: it is a real upstream model absent from the config")
	}
}

// TestReconcileAddsDiscoveredModel is the regression for Space-Bunny: a model
// upstream serves but the config does not list has to land in the catalog so the
// plane can route it. Without this the sync reports success and the model stays
// unusable.
func TestReconcileAddsDiscoveredModel(t *testing.T) {
	api, db := newTestAPI(t)
	report, err := api.reconcileModels(context.Background(), models.ProviderWorkBuddy, []probeOutcome{
		{ModelID: "space-bunny", Exists: true, Capabilities: []string{"chat", "streaming", "reasoning", "reasoning_effort"}},
	})
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if report.Added != 1 {
		t.Fatalf("added = %d, want 1", report.Added)
	}
	rows, err := db.ListCatalogModels(context.Background(), []string{models.ProviderWorkBuddy})
	if err != nil {
		t.Fatalf("list catalog: %v", err)
	}
	found := false
	for _, row := range rows {
		if row.ModelID != "space-bunny" {
			continue
		}
		found = true
		if row.Source != catalogSourceDiscovery {
			t.Fatalf("source = %q, want %q so the plane can merge it", row.Source, catalogSourceDiscovery)
		}
		if !row.Enabled {
			t.Fatal("a newly discovered model must be enabled")
		}
	}
	if !found {
		t.Fatal("space-bunny missing from the catalog after a successful probe")
	}
}

// TestReconcileDoesNotResurrectDisabledModel guards the operator's choice: a
// model they switched off must stay off across syncs, otherwise every sync
// silently re-enables it.
func TestReconcileDoesNotResurrectDisabledModel(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()
	if err := db.UpsertCatalogModel(ctx, store.CatalogModel{
		Provider: models.ProviderWorkBuddy, ModelID: "space-bunny", DisplayName: "space-bunny",
		Capabilities: []string{"chat", "streaming"}, Source: catalogSourceDiscovery, Enabled: false,
	}); err != nil {
		t.Fatalf("seed catalog: %v", err)
	}
	if _, err := api.reconcileModels(ctx, models.ProviderWorkBuddy, []probeOutcome{
		{ModelID: "space-bunny", Exists: true, Capabilities: []string{"chat", "streaming"}},
	}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	rows, err := db.ListCatalogModels(ctx, []string{models.ProviderWorkBuddy})
	if err != nil {
		t.Fatalf("list catalog: %v", err)
	}
	for _, row := range rows {
		if row.ModelID == "space-bunny" && row.Enabled {
			t.Fatal("a disabled model must stay disabled across a sync")
		}
	}
}

// TestReconcileLeavesConfigOwnedRowsAlone pins that discovery does not rewrite a
// row the config owns: a probe with a truncated output can miss reasoning, and
// letting that overwrite the config's capability would strip a real feature.
func TestReconcileLeavesConfigOwnedRowsAlone(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()
	if err := db.UpsertCatalogModel(ctx, store.CatalogModel{
		Provider: models.ProviderWorkBuddy, ModelID: "deepseek-v4-flash", DisplayName: "DeepSeek V4 Flash",
		Capabilities: []string{"chat", "streaming", "reasoning", "reasoning_effort"}, Source: "definition", Enabled: true,
	}); err != nil {
		t.Fatalf("seed catalog: %v", err)
	}
	if _, err := api.reconcileModels(ctx, models.ProviderWorkBuddy, []probeOutcome{
		{ModelID: "deepseek-v4-flash", Exists: true, Capabilities: []string{"chat", "streaming"}},
	}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	rows, err := db.ListCatalogModels(ctx, []string{models.ProviderWorkBuddy})
	if err != nil {
		t.Fatalf("list catalog: %v", err)
	}
	for _, row := range rows {
		if row.ModelID != "deepseek-v4-flash" {
			continue
		}
		if row.Source != "definition" {
			t.Fatalf("source = %q, want the config to keep ownership", row.Source)
		}
		if !equalCapabilities(row.Capabilities, []string{"chat", "streaming", "reasoning", "reasoning_effort"}) {
			t.Fatalf("capabilities = %v, want the config's set preserved", row.Capabilities)
		}
	}
}

// TestReconcileRemovesNothingOnProbeMiss pins the conservative half: a model the
// probe could not confirm must not be deleted, because a transient probe failure
// is indistinguishable from a retracted model.
func TestReconcileRemovesNothingOnProbeMiss(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()
	if err := db.UpsertCatalogModel(ctx, store.CatalogModel{
		Provider: models.ProviderWorkBuddy, ModelID: "hy3", DisplayName: "hy3",
		Capabilities: []string{"chat", "streaming"}, Source: catalogSourceDiscovery, Enabled: true,
	}); err != nil {
		t.Fatalf("seed catalog: %v", err)
	}
	if _, err := api.reconcileModels(ctx, models.ProviderWorkBuddy, []probeOutcome{
		{ModelID: "hy3", Exists: false},
	}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	rows, err := db.ListCatalogModels(ctx, []string{models.ProviderWorkBuddy})
	if err != nil {
		t.Fatalf("list catalog: %v", err)
	}
	found := false
	for _, row := range rows {
		if row.ModelID == "hy3" {
			found = true
		}
	}
	if !found {
		t.Fatal("an unconfirmed model must not be removed by a sync")
	}
}

// TestCapabilityFlagsRoundTrips pins the stored capability vocabulary against
// the flags the catalog builder reads, so a discovered model is routable with
// the capabilities upstream actually reported.
func TestCapabilityFlagsRoundTrips(t *testing.T) {
	flags := capabilityFlags([]string{"chat", "streaming", "reasoning", "reasoning_effort"})
	if !flags.Chat || !flags.Streaming || !flags.Reasoning || !flags.ReasoningEffort {
		t.Fatalf("flags = %+v, want every listed capability set", flags)
	}
	empty := capabilityFlags(nil)
	if empty.Chat || empty.Reasoning {
		t.Fatalf("flags = %+v, want nothing set for an empty list", empty)
	}
}

// TestReconcileNeverDowngradesDiscoveredCapabilities is the regression for a
// stochastic probe miss stripping a real capability. Measured 2026-10-06:
// space-bunny emits reasoning on 2 of 3 trivial prompts and glm-5.3-flash on 1
// of 4 reasoning prompts, so a single probe cannot prove a capability is gone.
// Letting a miss rewrite the row would silently drop reasoning from a model
// that has it.
func TestReconcileNeverDowngradesDiscoveredCapabilities(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()
	if err := db.UpsertCatalogModel(ctx, store.CatalogModel{
		Provider: models.ProviderWorkBuddy, ModelID: "space-bunny", DisplayName: "space-bunny",
		Capabilities: []string{"chat", "streaming", "reasoning", "reasoning_effort"},
		Source:       catalogSourceDiscovery, Enabled: true,
	}); err != nil {
		t.Fatalf("seed catalog: %v", err)
	}
	// This probe miss reports only chat+streaming.
	if _, err := api.reconcileModels(ctx, models.ProviderWorkBuddy, []probeOutcome{
		{ModelID: "space-bunny", Exists: true, Capabilities: []string{"chat", "streaming"}},
	}); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	rows, err := db.ListCatalogModels(ctx, []string{models.ProviderWorkBuddy})
	if err != nil {
		t.Fatalf("list catalog: %v", err)
	}
	for _, row := range rows {
		if row.ModelID != "space-bunny" {
			continue
		}
		if !equalCapabilities(row.Capabilities, []string{"chat", "streaming", "reasoning", "reasoning_effort"}) {
			t.Fatalf("capabilities = %v, want reasoning preserved across a probe miss", row.Capabilities)
		}
	}
}

// TestKeepDiscoveredCapabilities pins the union rule: additive capabilities are
// preserved, while a capability the probe did not report and the row never had
// is not invented.
func TestKeepDiscoveredCapabilities(t *testing.T) {
	merged := keepDiscoveredCapabilities(
		[]string{"chat", "streaming", "reasoning", "reasoning_effort"},
		[]string{"chat", "streaming"},
	)
	if !equalCapabilities(merged, []string{"chat", "streaming", "reasoning", "reasoning_effort"}) {
		t.Fatalf("merged = %v, want reasoning preserved", merged)
	}
	// Nothing to preserve stays a clean add.
	fresh := keepDiscoveredCapabilities(nil, []string{"chat", "streaming"})
	if !equalCapabilities(fresh, []string{"chat", "streaming"}) {
		t.Fatalf("merged = %v, want a clean add", fresh)
	}
	// A probe that finds reasoning keeps it.
	gained := keepDiscoveredCapabilities([]string{"chat"}, []string{"chat", "streaming", "reasoning"})
	if !equalCapabilities(gained, []string{"chat", "streaming", "reasoning"}) {
		t.Fatalf("merged = %v, want the new capability kept", gained)
	}
}
