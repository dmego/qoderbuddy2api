package server

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"

	"github.com/dmego/qoderbuddy2api/internal/providers"
	"github.com/dmego/qoderbuddy2api/internal/store"
)

// TestAccountWeightRoundTrip proves the console can set a per-account weight and
// read it back: the value feeds the weighted/fill-first strategies, so a save
// that silently dropped it would leave the operator's ratio inoperative.
func TestAccountWeightRoundTrip(t *testing.T) {
	api, db := newTestAPI(t)
	api.Plane = NewProxyPlane(api.Settings, api.Vault)
	seedRoutingAccounts(t, db)
	ctx := context.Background()

	recorder := adminRequest(t, api, http.MethodPatch, "/api/admin/accounts/codebuddy/cb-a",
		map[string]any{"weight": 400})
	if recorder.Code != http.StatusOK {
		t.Fatalf("patch weight: status %d (%s)", recorder.Code, recorder.Body.String())
	}
	stored, err := db.GetAccount(ctx, "codebuddy", "cb-a")
	if err != nil {
		t.Fatalf("read account: %v", err)
	}
	if stored.Weight != 400 {
		t.Fatalf("weight = %d, want 400", stored.Weight)
	}

	detail := adminRequest(t, api, http.MethodGet, "/api/admin/accounts/codebuddy/cb-a", nil)
	if detail.Code != http.StatusOK {
		t.Fatalf("account detail: status %d", detail.Code)
	}
	var view struct {
		Weight int `json:"weight"`
	}
	if err := json.Unmarshal(detail.Body.Bytes(), &view); err != nil {
		t.Fatalf("decode account: %v", err)
	}
	if view.Weight != 400 {
		t.Fatalf("account detail weight = %d, want 400", view.Weight)
	}

	// The routing grid is where the strategy is inspected, so it must carry the
	// weight next to each account.
	routing := adminRequest(t, api, http.MethodGet, "/api/admin/routing", nil)
	if routing.Code != http.StatusOK {
		t.Fatalf("routing: status %d", routing.Code)
	}
	if !routingShowsWeight(t, routing.Body.Bytes(), "auto", "codebuddy", "cb-a", 400) {
		t.Error("the routing grid does not report cb-a's weight")
	}

	// Out-of-range weights are refused instead of stored.
	for _, weight := range []int{0, -1, store.MaxAccountWeight + 1} {
		bad := adminRequest(t, api, http.MethodPatch, "/api/admin/accounts/codebuddy/cb-a",
			map[string]any{"weight": weight})
		if bad.Code != http.StatusBadRequest {
			t.Errorf("weight %d: status %d, want 400", weight, bad.Code)
		}
	}
}

// TestAccountStrategySettingReachesThePlane proves a saved strategy is applied
// live: the request path must not read SQLite, so the settings endpoint has to
// push the value into the plane itself.
func TestAccountStrategySettingReachesThePlane(t *testing.T) {
	api, _ := newTestAPI(t)
	api.Plane = NewProxyPlane(api.Settings, api.Vault)

	recorder := adminRequest(t, api, http.MethodPatch, "/api/admin/settings",
		map[string]any{"key": "routing.account_strategy", "value": "weighted", "value_version": 0})
	if recorder.Code != http.StatusOK {
		t.Fatalf("save strategy: status %d (%s)", recorder.Code, recorder.Body.String())
	}
	if got := api.Plane.RoutingConfig().Strategy; got != providers.StrategyWeighted {
		t.Fatalf("plane strategy = %q, want %q", got, providers.StrategyWeighted)
	}

	affinity := adminRequest(t, api, http.MethodPatch, "/api/admin/settings",
		map[string]any{"key": "routing.session_affinity", "value": true, "value_version": 0})
	if affinity.Code != http.StatusOK {
		t.Fatalf("save affinity: status %d (%s)", affinity.Code, affinity.Body.String())
	}
	if !api.Plane.RoutingConfig().SessionAffinity {
		t.Fatal("plane did not enable session affinity")
	}

	// An unknown strategy is refused: silently degrading to round-robin would
	// look like a saved setting that does nothing.
	bad := adminRequest(t, api, http.MethodPatch, "/api/admin/settings",
		map[string]any{"key": "routing.account_strategy", "value": "roundrobin", "value_version": 0})
	if bad.Code != http.StatusBadRequest {
		t.Fatalf("unknown strategy: status %d, want 400", bad.Code)
	}

	// The console reads the live state from the routing payload.
	routing := adminRequest(t, api, http.MethodGet, "/api/admin/routing", nil)
	if routing.Code != http.StatusOK {
		t.Fatalf("routing: status %d", routing.Code)
	}
	var payload struct {
		AccountRouting struct {
			Strategy        string `json:"strategy"`
			SessionAffinity bool   `json:"session_affinity"`
		} `json:"account_routing"`
	}
	if err := json.Unmarshal(routing.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode routing: %v", err)
	}
	if payload.AccountRouting.Strategy != providers.StrategyWeighted || !payload.AccountRouting.SessionAffinity {
		t.Fatalf("routing payload = %+v, want weighted + affinity", payload.AccountRouting)
	}
}

// TestAccountStrategiesAreExposed proves the console can render every control
// the settings page offers: an unlisted schema key renders no control at all.
func TestAccountStrategiesAreExposed(t *testing.T) {
	api, _ := newTestAPI(t)
	recorder := adminRequest(t, api, http.MethodGet, "/api/admin/settings", nil)
	if recorder.Code != http.StatusOK {
		t.Fatalf("status %d", recorder.Code)
	}
	var response struct {
		Schema map[string]map[string]any `json:"schema"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatalf("decode: %v", err)
	}
	for key, wantType := range map[string]string{
		"routing.account_strategy":             "str",
		"routing.session_affinity":             "bool",
		"routing.session_affinity_ttl_seconds": "int",
	} {
		entry, ok := response.Schema[key]
		if !ok {
			t.Fatalf("schema entry missing for %s", key)
		}
		if kind, _ := entry["type"].(string); kind != wantType {
			t.Errorf("%s type = %q, want %q", key, kind, wantType)
		}
	}
	if def, _ := response.Schema["routing.account_strategy"]["default"].(string); def != providers.StrategyRoundRobin {
		t.Errorf("default strategy = %q, want round_robin (feature off by default)", def)
	}
	if def, ok := response.Schema["routing.session_affinity"]["default"].(bool); !ok || def {
		t.Errorf("session affinity must default to off, got %v", def)
	}
}

// routingShowsWeight reports whether the routing payload carries one account's
// weight.
func routingShowsWeight(t *testing.T, body []byte, modelID, provider, accountID string, weight int) bool {
	t.Helper()
	var payload struct {
		Models []struct {
			ModelID string `json:"model_id"`
			Routes  []struct {
				Provider string `json:"provider"`
				Accounts []struct {
					AccountID string `json:"account_id"`
					Weight    int    `json:"weight"`
				} `json:"accounts"`
			} `json:"routes"`
		} `json:"models"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		t.Fatalf("decode routing: %v", err)
	}
	for _, model := range payload.Models {
		if model.ModelID != modelID {
			continue
		}
		for _, route := range model.Routes {
			if route.Provider != provider {
				continue
			}
			for _, account := range route.Accounts {
				if account.AccountID == accountID {
					return account.Weight == weight
				}
			}
		}
	}
	return false
}
