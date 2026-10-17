package providers

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/chatwire"
	"github.com/dmego/qoderbuddy2api/internal/models"
)

// failingProvider refuses every request, so the router has to fail over.
type failingProvider struct{ name string }

func (f *failingProvider) Name() string { return f.name }

func (f *failingProvider) Complete(context.Context, *chatwire.ChatRequest) (map[string]any, error) {
	return nil, errors.New("upstream refused")
}

func (f *failingProvider) Stream(context.Context, *chatwire.ChatRequest) (FrameStream, error) {
	return nil, errors.New("upstream refused")
}

func (f *failingProvider) Close() error { return nil }

// recordingProvider reports which account served a request through the request's
// telemetry fields.
type recordingProvider struct{ name, account string }

func (r *recordingProvider) Name() string { return r.name }

func (r *recordingProvider) Complete(_ context.Context, request *chatwire.ChatRequest) (map[string]any, error) {
	request.RecordSlot(r.account, false)
	return map[string]any{"served_by": r.account}, nil
}

func (r *recordingProvider) Stream(_ context.Context, request *chatwire.ChatRequest) (FrameStream, error) {
	request.RecordSlot(r.account, false)
	return &emptyStream{}, nil
}

func (r *recordingProvider) Close() error { return nil }

// TestRouterSessionAffinityPinsTheProvider proves affinity spans the provider
// axis: an even provider weight split would otherwise send the second request of
// a turn to the other provider, and the account pin below it would never be
// consulted.
func TestRouterSessionAffinityPinsTheProvider(t *testing.T) {
	domestic := NewPool(models.ProviderWorkBuddy)
	domestic.UpdateSlots(map[string]Provider{
		"codebuddy:a": &recordingProvider{name: "codebuddy", account: "codebuddy:a"},
		"codebuddy:b": &recordingProvider{name: "codebuddy", account: "codebuddy:b"},
	})
	intl := NewPool(models.ProviderWorkBuddyIntl)
	intl.UpdateSlots(map[string]Provider{
		"workbuddy_intl:x": &recordingProvider{name: "workbuddy_intl", account: "workbuddy_intl:x"},
		"workbuddy_intl:y": &recordingProvider{name: "workbuddy_intl", account: "workbuddy_intl:y"},
	})
	pools := map[string]*Pool{models.ProviderWorkBuddy: domestic, models.ProviderWorkBuddyIntl: intl}

	affinity := NewAffinity()
	affinity.Configure(true, time.Minute)
	for _, pool := range pools {
		pool.SetAffinity(affinity)
	}

	catalog := map[string]models.Unified{
		"shared": {
			ID: "shared",
			Routes: []models.Route{
				{Provider: models.ProviderWorkBuddy, UpstreamID: "shared"},
				{Provider: models.ProviderWorkBuddyIntl, UpstreamID: "Shared"},
			},
		},
	}
	policies := map[string][]RoutePolicy{
		"shared": {
			{Provider: models.ProviderWorkBuddyIntl, Weight: 1, Enabled: true},
			{Provider: models.ProviderWorkBuddy, Weight: 1, Enabled: true},
		},
	}
	router := NewRouter(catalog, pools, policies, affinity)

	turn := &chatwire.ChatRequest{
		Model: "shared",
		Messages: []chatwire.Message{
			{Role: "user", Content: []byte(`"question"`)},
		},
	}
	first, err := router.Complete(context.Background(), turn)
	if err != nil {
		t.Fatalf("first request: %v", err)
	}
	served := first["served_by"]
	for range 6 {
		again := &chatwire.ChatRequest{Model: "shared", Messages: turn.Messages}
		result, err := router.Complete(context.Background(), again)
		if err != nil {
			t.Fatalf("repeat request: %v", err)
		}
		if result["served_by"] != served {
			t.Fatalf("affinity moved the conversation from %v to %v", served, result["served_by"])
		}
	}
}

// TestRouterAffinityFallsBackOnProviderFailure proves the pin never traps a
// request: a pinned provider that fails must be replaced, not retried forever.
func TestRouterAffinityFallsBackOnProviderFailure(t *testing.T) {
	broken := NewPool(models.ProviderWorkBuddy)
	broken.UpdateSlots(map[string]Provider{
		"codebuddy:broken": &failingProvider{name: "codebuddy"},
	})
	healthy := NewPool(models.ProviderWorkBuddyIntl)
	healthy.UpdateSlots(map[string]Provider{
		"workbuddy_intl:ok": &recordingProvider{name: "workbuddy_intl", account: "workbuddy_intl:ok"},
	})
	pools := map[string]*Pool{models.ProviderWorkBuddy: broken, models.ProviderWorkBuddyIntl: healthy}

	affinity := NewAffinity()
	affinity.Configure(true, time.Minute)
	for _, pool := range pools {
		pool.SetAffinity(affinity)
	}
	catalog := map[string]models.Unified{
		"shared": {
			ID: "shared",
			Routes: []models.Route{
				{Provider: models.ProviderWorkBuddy, UpstreamID: "shared"},
				{Provider: models.ProviderWorkBuddyIntl, UpstreamID: "Shared"},
			},
		},
	}
	// The broken provider is tried first, so the pin it leaves behind must be
	// dropped on failure.
	policies := map[string][]RoutePolicy{
		"shared": {
			{Provider: models.ProviderWorkBuddy, Priority: 0, Weight: 1, Enabled: true},
			{Provider: models.ProviderWorkBuddyIntl, Priority: 1, Weight: 1, Enabled: true},
		},
	}
	router := NewRouter(catalog, pools, policies, affinity)

	request := &chatwire.ChatRequest{
		Model:    "shared",
		Messages: []chatwire.Message{{Role: "user", Content: []byte(`"question"`)}},
	}
	result, err := router.Complete(context.Background(), request)
	if err != nil {
		t.Fatalf("router did not fail over: %v", err)
	}
	if result["served_by"] != "workbuddy_intl:ok" {
		t.Fatalf("served by %v, want the healthy provider", result["served_by"])
	}
	// The failed provider must not remain pinned; the pin is replaced by the
	// account that actually served the turn.
	pinned, ok := affinity.Pinned(request.SessionKey())
	if ok && providerOfSlotKey(pinned) == models.ProviderWorkBuddy {
		t.Fatalf("the failed provider stayed pinned as %q", pinned)
	}
	if !ok || pinned != "workbuddy_intl:ok" {
		t.Fatalf("pin = %q (present %v), want the account that served the turn", pinned, ok)
	}
}
