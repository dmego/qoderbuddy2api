package providers

import (
	"context"
	"errors"
	"testing"

	"github.com/dmego/qoderbuddy2api/internal/chatwire"
	"github.com/dmego/qoderbuddy2api/internal/models"
)

// cancelledProvider fails the way an upstream call does when the client hangs
// up: the request context is already cancelled by the time the error surfaces.
type cancelledProvider struct{ cancel context.CancelFunc }

func (c *cancelledProvider) Name() string { return "codebuddy" }

func (c *cancelledProvider) Complete(context.Context, *chatwire.ChatRequest) (map[string]any, error) {
	c.cancel()
	return nil, errors.New(`Post "https://copilot.tencent.com/v2/chat/completions": context canceled`)
}

func (c *cancelledProvider) Stream(context.Context, *chatwire.ChatRequest) (FrameStream, error) {
	c.cancel()
	return nil, errors.New(`Post "https://copilot.tencent.com/v2/chat/completions": context canceled`)
}

func (c *cancelledProvider) Close() error { return nil }

// TestClientAbortDoesNotCoolDownTheAccount pins the regression behind the
// "503 no available routes" reports.
//
// A request the caller abandoned says nothing about account health. The build
// used to run the ordinary failure path on it, which cooled the account and its
// provider for 30 seconds; with four accounts and two providers, a handful of
// cancelled requests were enough to empty every route and answer 503 to every
// later request until the cooldown expired.
func TestClientAbortDoesNotCoolDownTheAccount(t *testing.T) {
	for _, mode := range []string{"complete", "stream"} {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			pool := NewPool("codebuddy")
			pool.UpdateSlots(map[string]Provider{
				"codebuddy:aborting": &cancelledProvider{cancel: cancel},
			})

			request := &chatwire.ChatRequest{Model: "m"}
			var err error
			if mode == "complete" {
				_, err = pool.Complete(ctx, request)
			} else {
				_, err = pool.Stream(ctx, request)
			}
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("error = %v, want the cancellation itself", err)
			}

			pool.mu.Lock()
			_, cooling := pool.failed["codebuddy:aborting"]
			pool.mu.Unlock()
			if cooling {
				t.Fatal("a client abort put the account into its failure cooldown")
			}
		})
	}
}

// TestGenuineFailureStillCoolsDown proves the guard is narrow: a real upstream
// rejection must still take the account out of rotation.
func TestGenuineFailureStillCoolsDown(t *testing.T) {
	pool := NewPool("codebuddy")
	pool.UpdateSlots(map[string]Provider{
		"codebuddy:broken": &failingProvider{name: "codebuddy"},
	})

	if _, err := pool.Complete(context.Background(), &chatwire.ChatRequest{Model: "m"}); err == nil {
		t.Fatal("expected the upstream failure")
	}
	pool.mu.Lock()
	until, cooling := pool.failed["codebuddy:broken"]
	pool.mu.Unlock()
	if !cooling || until <= nowMonotonic() {
		t.Fatal("a genuine upstream failure did not cool the account down")
	}
}

// TestRouterClientAbortDoesNotCoolDownTheRoute pins the same rule one level up:
// a cancelled request must not put the provider route into its cooldown, which
// is what produced "no available routes" for the whole model.
func TestRouterClientAbortDoesNotCoolDownTheRoute(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	pool := NewPool("codebuddy")
	pool.UpdateSlots(map[string]Provider{
		"codebuddy:aborting": &cancelledProvider{cancel: cancel},
	})
	pools := map[string]*Pool{"codebuddy": pool}
	catalog := map[string]models.Unified{
		"shared": {
			ID:     "shared",
			Routes: []models.Route{{Provider: "codebuddy", UpstreamID: "shared"}},
		},
	}
	router := NewRouter(catalog, pools, nil, nil)

	request := &chatwire.ChatRequest{
		Model:    "shared",
		Messages: []chatwire.Message{{Role: "user", Content: []byte(`"hi"`)}},
	}
	if _, err := router.Complete(ctx, request); !errors.Is(err, context.Canceled) {
		t.Fatalf("error = %v, want the cancellation", err)
	}
	router.mu.Lock()
	until, cooling := router.cooldown[[2]string{"shared", "codebuddy"}]
	router.mu.Unlock()
	if cooling && until > nowMonotonic() {
		t.Fatal("a client abort put the provider route into its cooldown")
	}
}
