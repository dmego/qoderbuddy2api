package models

import (
	"os"
	"path/filepath"
	"testing"
)

func TestBuildCatalogMergesSharedModels(t *testing.T) {
	perProvider := map[string][]Definition{
		ProviderWorkBuddy: {
			{ID: "deepseek-v4.1-flash", Name: "DeepSeek V4.1 Flash", Provider: ProviderWorkBuddy},
			{ID: "GLM-5.2", Name: "GLM-5.2", Provider: ProviderWorkBuddy},
		},
		ProviderWorkBuddyIntl: {
			{ID: "DeepSeek-V4.1-Flash", Name: "Intl Flash", Provider: ProviderWorkBuddyIntl},
			{ID: "hy3", Name: "Hy3", Provider: ProviderWorkBuddyIntl},
		},
	}
	catalog := BuildCatalog(perProvider, nil)

	shared, ok := catalog["deepseek-v4.1-flash"]
	if !ok {
		t.Fatalf("expected merged entry, got keys %v", keys(catalog))
	}
	if len(shared.Routes) != 2 {
		t.Fatalf("expected one route per provider, got %#v", shared.Routes)
	}
	// Domestic route must win the display name (models_catalog._preferred_name).
	if shared.Name != "DeepSeek V4.1 Flash" {
		t.Fatalf("preferred name should come from the domestic pool, got %q", shared.Name)
	}

	onlyIntl, ok := catalog["hy3"]
	if !ok || len(onlyIntl.Routes) != 1 || onlyIntl.Routes[0].Provider != ProviderWorkBuddyIntl {
		t.Fatalf("provider-exclusive model must keep a single route: %#v", onlyIntl)
	}

	lower, ok := catalog["glm-5.2"]
	if !ok {
		t.Fatalf("canonical ids are lowercase; catalog keys: %v", keys(catalog))
	}
	if lower.Routes[0].UpstreamID != "GLM-5.2" {
		t.Fatalf("upstream id must stay verbatim, got %q", lower.Routes[0].UpstreamID)
	}
}

func TestLoadDefinitionsFallsBackToBuiltinSets(t *testing.T) {
	defs := LoadDefinitions(filepath.Join(t.TempDir(), "missing.json"))
	if len(defs[ProviderWorkBuddy]) == 0 || len(defs[ProviderWorkBuddyIntl]) == 0 {
		t.Fatalf("missing config must fall back to built-in definitions: %#v", defs)
	}
}

func TestLoadDefinitionsReadsConfigFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "models.json")
	body := `{"codebuddy":{"models":[{"id":"alpha","name":"Alpha","capabilities":{"chat":true,"streaming":true}}]},
	          "workbuddy_intl":{"models":[{"id":"beta","name":"Beta"}]}}`
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	defs := LoadDefinitions(path)
	if len(defs[ProviderWorkBuddy]) != 1 || defs[ProviderWorkBuddy][0].ID != "alpha" {
		t.Fatalf("domestic definitions not parsed: %#v", defs[ProviderWorkBuddy])
	}
	if len(defs[ProviderWorkBuddyIntl]) != 1 || defs[ProviderWorkBuddyIntl][0].Name != "Beta" {
		t.Fatalf("intl definitions not parsed: %#v", defs[ProviderWorkBuddyIntl])
	}
}

func keys(m map[string]Unified) []string {
	out := make([]string, 0, len(m))
	for key := range m {
		out = append(out, key)
	}
	return out
}
