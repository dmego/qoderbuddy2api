// Package models holds model definitions, the unified catalog and route policy.
package models

import (
	"encoding/json"
	"os"
	"sort"
	"strings"
)

// Provider names kept by the rewrite. The domestic WorkBuddy deployment is
// called "codebuddy" everywhere in the database, the API and the frontend
// (accounts were imported under that name), so the identifier is preserved even
// though the product is WorkBuddy — renaming it would orphan every account row.
const (
	ProviderWorkBuddy     = "codebuddy"
	ProviderWorkBuddyIntl = "workbuddy_intl"
)

// KnownProviders lists the providers the rewrite supports, in catalog order.
var KnownProviders = []string{ProviderWorkBuddyIntl, ProviderWorkBuddy}

// ChatOnlyProviders never accrue sign-in or growth-centre state.
var ChatOnlyProviders = map[string]bool{ProviderWorkBuddyIntl: true}

// Capabilities mirrors models.ModelCapabilities.
type Capabilities struct {
	Chat            bool `json:"chat"`
	Streaming       bool `json:"streaming"`
	ToolCalling     bool `json:"tool_calling"`
	Reasoning       bool `json:"reasoning"`
	ReasoningEffort bool `json:"reasoning_effort"`
	ContextWindow   bool `json:"context_window"`
	MaxOutputTokens bool `json:"max_output_tokens"`
}

// Definition mirrors models.ModelDefinition.
type Definition struct {
	ID           string
	Name         string
	Provider     string
	Capabilities Capabilities
	MaxContext   int
	MaxOutput    int
	Metadata     map[string]any
}

// DefaultWorkBuddyModels is used when config/models.json is absent or unusable.
var DefaultWorkBuddyModels = []Definition{
	{ID: "auto", Name: "Auto", Provider: ProviderWorkBuddy, Capabilities: Capabilities{Chat: true, Streaming: true}},
	{ID: "deepseek-v3", Name: "DeepSeek V3", Provider: ProviderWorkBuddy, Capabilities: Capabilities{Chat: true, Streaming: true, ToolCalling: true}},
	{ID: "deepseek-v3-0324", Name: "DeepSeek V3 (0324)", Provider: ProviderWorkBuddy, Capabilities: Capabilities{Chat: true, Streaming: true}},
	{ID: "deepseek-v4-pro", Name: "DeepSeek V4 Pro", Provider: ProviderWorkBuddy, Capabilities: Capabilities{Chat: true, Streaming: true, Reasoning: true, ReasoningEffort: true}},
	{ID: "deepseek-v4-flash", Name: "DeepSeek V4 Flash", Provider: ProviderWorkBuddy, Capabilities: Capabilities{Chat: true, Streaming: true, Reasoning: true, ReasoningEffort: true}},
	{ID: "deepseek-r1", Name: "DeepSeek R1", Provider: ProviderWorkBuddy, Capabilities: Capabilities{Chat: true, Streaming: true, Reasoning: true}},
	{ID: "glm-5.1", Name: "GLM-5.1", Provider: ProviderWorkBuddy, Capabilities: Capabilities{Chat: true, Streaming: true}},
	{ID: "glm-5.2", Name: "GLM-5.2", Provider: ProviderWorkBuddy, Capabilities: Capabilities{Chat: true, Streaming: true}},
	{ID: "glm-5v-turbo", Name: "GLM-5v-Turbo", Provider: ProviderWorkBuddy, Capabilities: Capabilities{Chat: true, Streaming: true}},
	{ID: "minimax-m3", Name: "MiniMax M3", Provider: ProviderWorkBuddy, Capabilities: Capabilities{Chat: true, Streaming: true}},
	{ID: "minimax-m2.7", Name: "MiniMax M2.7", Provider: ProviderWorkBuddy, Capabilities: Capabilities{Chat: true, Streaming: true, Reasoning: true}},
	{ID: "kimi-k2.6", Name: "Kimi K2.6", Provider: ProviderWorkBuddy, Capabilities: Capabilities{Chat: true, Streaming: true}},
	{ID: "kimi-k2.7", Name: "Kimi K2.7", Provider: ProviderWorkBuddy, Capabilities: Capabilities{Chat: true, Streaming: true, Reasoning: true}},
	{ID: "hy3", Name: "Hy3", Provider: ProviderWorkBuddy, Capabilities: Capabilities{Chat: true, Streaming: true}},
}

// DefaultIntlModels is the international free tier.
var DefaultIntlModels = []Definition{
	{ID: "hy4-preview", Name: "Hy4 Preview", Provider: ProviderWorkBuddyIntl, Capabilities: Capabilities{Chat: true, Streaming: true, Reasoning: true, ReasoningEffort: true, ToolCalling: true}},
	{ID: "hy3", Name: "Hy3", Provider: ProviderWorkBuddyIntl, Capabilities: Capabilities{Chat: true, Streaming: true, Reasoning: true, ReasoningEffort: true, ToolCalling: true}},
	{ID: "deepseek-v4.1-flash", Name: "DeepSeek V4.1 Flash", Provider: ProviderWorkBuddyIntl, Capabilities: Capabilities{Chat: true, Streaming: true, Reasoning: true, ReasoningEffort: true, ToolCalling: true}},
}

// LoadDefinitions reads config/models.json, falling back to the built-in sets.
func LoadDefinitions(path string) map[string][]Definition {
	result := map[string][]Definition{
		ProviderWorkBuddy:     append([]Definition(nil), DefaultWorkBuddyModels...),
		ProviderWorkBuddyIntl: append([]Definition(nil), DefaultIntlModels...),
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return result
	}
	var doc struct {
		Codebuddy struct {
			Models []json.RawMessage `json:"models"`
		} `json:"codebuddy"`
		Intl struct {
			Models []json.RawMessage `json:"models"`
		} `json:"workbuddy_intl"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return result
	}
	if parsed := parseDefinitions(ProviderWorkBuddy, doc.Codebuddy.Models); len(parsed) > 0 {
		result[ProviderWorkBuddy] = parsed
	}
	if parsed := parseDefinitions(ProviderWorkBuddyIntl, doc.Intl.Models); len(parsed) > 0 {
		result[ProviderWorkBuddyIntl] = parsed
	}
	return result
}

// LoadUnifiedOverrides returns the optional top-level "unified" config section.
func LoadUnifiedOverrides(path string) map[string]UnifiedOverride {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var doc struct {
		Unified map[string]UnifiedOverride `json:"unified"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil
	}
	return doc.Unified
}

type rawModel struct {
	ID           string          `json:"id"`
	Name         string          `json:"name"`
	Capabilities map[string]bool `json:"capabilities"`
	MaxContext   *int            `json:"max_context"`
	MaxOutput    *int            `json:"max_output"`
	Metadata     map[string]any  `json:"metadata"`
}

func parseDefinitions(provider string, entries []json.RawMessage) []Definition {
	var out []Definition
	for _, entry := range entries {
		var raw rawModel
		if err := json.Unmarshal(entry, &raw); err != nil || raw.ID == "" {
			continue
		}
		name := raw.Name
		if name == "" {
			name = raw.ID
		}
		definition := Definition{
			ID:       raw.ID,
			Name:     name,
			Provider: provider,
			Capabilities: Capabilities{
				Chat:            capValue(raw.Capabilities, "chat", true),
				Streaming:       capValue(raw.Capabilities, "streaming", true),
				ToolCalling:     capValue(raw.Capabilities, "tool_calling", false),
				Reasoning:       capValue(raw.Capabilities, "reasoning", false),
				ReasoningEffort: capValue(raw.Capabilities, "reasoning_effort", false),
				ContextWindow:   capValue(raw.Capabilities, "context_window", false),
				MaxOutputTokens: capValue(raw.Capabilities, "max_output_tokens", false),
			},
			MaxContext: 128000,
			MaxOutput:  4096,
			Metadata:   raw.Metadata,
		}
		if raw.MaxContext != nil {
			definition.MaxContext = *raw.MaxContext
		}
		if raw.MaxOutput != nil {
			definition.MaxOutput = *raw.MaxOutput
		}
		out = append(out, definition)
	}
	return out
}

func capValue(values map[string]bool, key string, fallback bool) bool {
	if values == nil {
		return fallback
	}
	if value, ok := values[key]; ok {
		return value
	}
	return fallback
}

// Route is one provider backend able to serve a unified model.
type Route struct {
	Provider   string
	UpstreamID string
}

// Unified is the public catalog entry for one canonical model id.
type Unified struct {
	ID           string
	Name         string
	Capabilities Capabilities
	MaxContext   int
	MaxOutput    int
	Routes       []Route
}

// UnifiedOverride is one entry of the config "unified" section.
type UnifiedOverride struct {
	Name         string         `json:"name"`
	Capabilities map[string]any `json:"capabilities"`
	Routes       []struct {
		Provider   string `json:"provider"`
		UpstreamID string `json:"upstream_id"`
	} `json:"routes"`
}

// NormalizeID is the canonical (lowercase, trimmed) form of a model id.
func NormalizeID(id string) string { return strings.ToLower(strings.TrimSpace(id)) }

// BuildCatalog merges per-provider definitions into the unified catalog.
// Models whose normalized ids collide are merged into one entry with one route
// per provider, matching src/qb2api/models_catalog.py.
func BuildCatalog(perProvider map[string][]Definition, overrides map[string]UnifiedOverride) map[string]Unified {
	grouped := map[string][]Definition{}
	for _, provider := range KnownProviders {
		for _, definition := range perProvider[provider] {
			canonical := NormalizeID(definition.ID)
			grouped[canonical] = append(grouped[canonical], definition)
		}
	}
	merged := map[string]Unified{}
	for canonical, definitions := range grouped {
		merged[canonical] = mergeDefinitions(canonical, definitions)
	}
	for canonical, override := range overrides {
		merged[canonical] = applyOverride(canonical, merged[canonical], override)
	}
	return merged
}

func mergeDefinitions(canonical string, definitions []Definition) Unified {
	ordered := append([]Definition(nil), definitions...)
	sort.SliceStable(ordered, func(i, j int) bool {
		return providerRank(ordered[i].Provider) < providerRank(ordered[j].Provider)
	})
	entry := Unified{
		ID:           canonical,
		Name:         preferredName(ordered),
		Capabilities: unionCapabilities(ordered),
	}
	for _, definition := range ordered {
		if definition.MaxContext > entry.MaxContext {
			entry.MaxContext = definition.MaxContext
		}
		if definition.MaxOutput > entry.MaxOutput {
			entry.MaxOutput = definition.MaxOutput
		}
		entry.Routes = append(entry.Routes, Route{
			Provider:   definition.Provider,
			UpstreamID: upstreamID(definition),
		})
	}
	return entry
}

func applyOverride(canonical string, base Unified, override UnifiedOverride) Unified {
	entry := base
	entry.ID = canonical
	if override.Name != "" {
		entry.Name = override.Name
	} else if entry.Name == "" {
		entry.Name = canonical
	}
	if entry.MaxContext == 0 {
		entry.MaxContext = 128000
	}
	if entry.MaxOutput == 0 {
		entry.MaxOutput = 4096
	}
	if override.Capabilities != nil {
		entry.Capabilities = Capabilities{
			Chat:            boolValue(override.Capabilities["chat"]),
			Streaming:       boolValue(override.Capabilities["streaming"]),
			ToolCalling:     boolValue(override.Capabilities["tool_calling"]),
			Reasoning:       boolValue(override.Capabilities["reasoning"]),
			ReasoningEffort: boolValue(override.Capabilities["reasoning_effort"]),
			ContextWindow:   boolValue(override.Capabilities["context_window"]),
			MaxOutputTokens: boolValue(override.Capabilities["max_output_tokens"]),
		}
	}
	if len(override.Routes) > 0 {
		routes := make([]Route, 0, len(override.Routes))
		for _, route := range override.Routes {
			if route.Provider != "" && route.UpstreamID != "" {
				routes = append(routes, Route{Provider: route.Provider, UpstreamID: route.UpstreamID})
			}
		}
		if len(routes) > 0 {
			entry.Routes = routes
		}
	}
	return entry
}

func unionCapabilities(definitions []Definition) Capabilities {
	var out Capabilities
	for _, definition := range definitions {
		out.Chat = out.Chat || definition.Capabilities.Chat
		out.Streaming = out.Streaming || definition.Capabilities.Streaming
		out.ToolCalling = out.ToolCalling || definition.Capabilities.ToolCalling
		out.Reasoning = out.Reasoning || definition.Capabilities.Reasoning
		out.ReasoningEffort = out.ReasoningEffort || definition.Capabilities.ReasoningEffort
		out.ContextWindow = out.ContextWindow || definition.Capabilities.ContextWindow
		out.MaxOutputTokens = out.MaxOutputTokens || definition.Capabilities.MaxOutputTokens
	}
	return out
}

func providerRank(provider string) int {
	for i, name := range KnownProviders {
		if name == provider {
			return i
		}
	}
	return len(KnownProviders)
}

func preferredName(definitions []Definition) string {
	for _, definition := range definitions {
		if definition.Provider == ProviderWorkBuddy {
			return definition.Name
		}
	}
	if len(definitions) > 0 {
		return definitions[0].Name
	}
	return ""
}

func upstreamID(definition Definition) string {
	if definition.Metadata != nil {
		if value, ok := definition.Metadata["upstream_id"].(string); ok && value != "" {
			return value
		}
	}
	return definition.ID
}

func boolValue(value any) bool {
	flag, _ := value.(bool)
	return flag
}
