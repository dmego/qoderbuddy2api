package server

import (
	"context"
	"encoding/json"
	"net/http"
	"testing"

	"github.com/dmego/qoderbuddy2api/internal/store"
)

// seedRoutingAccounts creates the chat-capable accounts the routing grid shows.
func seedRoutingAccounts(t *testing.T, db *store.DB) {
	t.Helper()
	ctx := context.Background()
	for _, account := range []store.Account{
		{Provider: "codebuddy", AccountID: "cb-a", Label: "A", Source: "oauth", Enabled: true},
		{Provider: "codebuddy", AccountID: "cb-b", Label: "B", Source: "oauth", Enabled: true},
	} {
		if _, err := db.UpsertAccount(ctx, account); err != nil {
			t.Fatalf("seed account: %v", err)
		}
		if err := db.UpsertPurpose(ctx, store.Purpose{
			Provider: "codebuddy", AccountID: account.AccountID,
			Purpose: "chat", Enabled: true, Status: "active",
		}); err != nil {
			t.Fatalf("seed purpose: %v", err)
		}
	}
}

// TestAccountBlockEndpointRoundTrip proves the console's per-account checkbox
// works end to end: the PUT persisted a manual exclusion, the routing grid
// reported it as blocked, and unblocking cleared the row again.
//
// The route was missing entirely — the console called
// PUT /routing/{model}/accounts/{provider}/{account} and the mux answered
// "404 page not found", so the toggle in the UI could never succeed.
func TestAccountBlockEndpointRoundTrip(t *testing.T) {
	api, db := newTestAPI(t)
	api.Plane = NewProxyPlane(api.Settings, api.Vault)
	seedRoutingAccounts(t, db)
	ctx := context.Background()

	target := "/api/admin/routing/auto/accounts/codebuddy/cb-a"
	recorder := adminRequest(t, api, http.MethodPut, target, map[string]any{
		"blocked": true, "reason": "管理员手动停用",
	})
	if recorder.Code != http.StatusOK {
		t.Fatalf("block: status %d (%s)", recorder.Code, recorder.Body.String())
	}
	var blocked struct {
		Status    string `json:"status"`
		Blocked   bool   `json:"blocked"`
		ModelID   string `json:"model_id"`
		Provider  string `json:"provider"`
		AccountID string `json:"account_id"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &blocked); err != nil {
		t.Fatalf("decode block response: %v", err)
	}
	if blocked.Status != "ok" || !blocked.Blocked || blocked.ModelID != "auto" ||
		blocked.Provider != "codebuddy" || blocked.AccountID != "cb-a" {
		t.Fatalf("unexpected block response: %+v", blocked)
	}

	// The row is what the request path uses: ProxyPlane.Reload folds
	// source='manual' rows into the pool as hard blocks.
	rows, err := db.ListAccountModelBlocks(ctx)
	if err != nil {
		t.Fatalf("list blocks: %v", err)
	}
	if len(rows) != 1 || rows[0].Source != "manual" || rows[0].Reason != "管理员手动停用" {
		t.Fatalf("stored manual block = %+v, want one manual row for cb-a/auto", rows)
	}

	// And the console reads it back as blocked.
	routing := adminRequest(t, api, http.MethodGet, "/api/admin/routing", nil)
	if routing.Code != http.StatusOK {
		t.Fatalf("routing: status %d", routing.Code)
	}
	if !routingShowsBlocked(t, routing.Body.Bytes(), "auto", "codebuddy", "cb-a") {
		t.Error("the routing grid does not report cb-a as blocked after the PUT")
	}

	// Unblocking clears whatever row exists, manual or auto.
	recorder = adminRequest(t, api, http.MethodPut, target, map[string]any{"blocked": false})
	if recorder.Code != http.StatusOK {
		t.Fatalf("unblock: status %d (%s)", recorder.Code, recorder.Body.String())
	}
	rows, err = db.ListAccountModelBlocks(ctx)
	if err != nil {
		t.Fatalf("list blocks after unblock: %v", err)
	}
	if len(rows) != 0 {
		t.Fatalf("blocks after unblock = %+v, want none", rows)
	}
	routing = adminRequest(t, api, http.MethodGet, "/api/admin/routing", nil)
	if routingShowsBlocked(t, routing.Body.Bytes(), "auto", "codebuddy", "cb-a") {
		t.Error("cb-a still reported blocked after unblock")
	}
}

// TestAccountBlockRejectsInvalidTarget proves the endpoint refuses exclusions
// that no route could honour, instead of storing dead rows.
func TestAccountBlockRejectsInvalidTarget(t *testing.T) {
	api, db := newTestAPI(t)
	api.Plane = NewProxyPlane(api.Settings, api.Vault)
	seedRoutingAccounts(t, db)

	cases := []struct {
		name   string
		target string
		body   any
		want   int
	}{
		{
			"unknown model",
			"/api/admin/routing/no-such-model/accounts/codebuddy/cb-a",
			map[string]any{"blocked": true}, http.StatusNotFound,
		},
		{
			"provider does not serve the model",
			"/api/admin/routing/auto/accounts/workbuddy_intl/wbintl-1",
			map[string]any{"blocked": true}, http.StatusBadRequest,
		},
		{
			"missing blocked flag",
			"/api/admin/routing/auto/accounts/codebuddy/cb-a",
			map[string]any{"reason": "x"}, http.StatusBadRequest,
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			recorder := adminRequest(t, api, http.MethodPut, testCase.target, testCase.body)
			if recorder.Code != testCase.want {
				t.Fatalf("status %d, want %d (%s)", recorder.Code, testCase.want, recorder.Body.String())
			}
			// The console reads `detail` for the red toast.
			var payload struct {
				Detail string `json:"detail"`
			}
			if err := json.Unmarshal(recorder.Body.Bytes(), &payload); err != nil {
				t.Fatalf("error body is not JSON: %s", recorder.Body.String())
			}
			if payload.Detail == "" {
				t.Errorf("error body has no detail: %s", recorder.Body.String())
			}
			rows, err := db.ListAccountModelBlocks(context.Background())
			if err != nil {
				t.Fatalf("list blocks: %v", err)
			}
			if len(rows) != 0 {
				t.Errorf("a rejected request stored a block: %+v", rows)
			}
		})
	}
}

// routingShowsBlocked reports whether the routing payload marks one account as
// blocked for one model.
func routingShowsBlocked(t *testing.T, body []byte, modelID, provider, accountID string) bool {
	t.Helper()
	var payload struct {
		Models []struct {
			ModelID string `json:"model_id"`
			Routes  []struct {
				Provider string `json:"provider"`
				Accounts []struct {
					AccountID string `json:"account_id"`
					Blocked   bool   `json:"blocked"`
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
					return account.Blocked
				}
			}
		}
	}
	t.Fatalf("account %s not present on %s/%s", accountID, modelID, provider)
	return false
}
