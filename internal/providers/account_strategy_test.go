package providers

import (
	"context"
	"io"
	"testing"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/chatwire"
)

// stubProvider is a credential that records nothing; the strategy tests only
// exercise account selection, never the upstream call.
type stubProvider struct{ name string }

func (s *stubProvider) Name() string { return s.name }

func (s *stubProvider) Complete(context.Context, *chatwire.ChatRequest) (map[string]any, error) {
	return map[string]any{}, nil
}

func (s *stubProvider) Stream(context.Context, *chatwire.ChatRequest) (FrameStream, error) {
	return &emptyStream{}, nil
}

func (s *stubProvider) Close() error { return nil }

type emptyStream struct{}

func (s *emptyStream) Next() ([]byte, error) { return nil, io.EOF }
func (s *emptyStream) Close() error          { return nil }

// newStrategyPool builds a pool with the given slot keys and weights.
func newStrategyPool(t *testing.T, strategy string, weights map[string]int) *Pool {
	t.Helper()
	providers := map[string]Provider{}
	for key := range weights {
		providers[key] = &stubProvider{name: key}
	}
	pool := NewPool("codebuddy")
	pool.UpdateSlots(providers)
	pool.SetStrategy(strategy)
	pool.SetWeights(weights)
	return pool
}

// draw runs one acquire+release cycle and reports the selected slot key.
func draw(t *testing.T, pool *Pool, model, session string) string {
	t.Helper()
	handle := pool.acquire(map[string]bool{}, model, session, true)
	if handle == nil {
		t.Fatal("acquire returned no slot")
	}
	key := handle.key
	pool.release(handle)
	return key
}

// TestRoundRobinIsTheDefault proves the untouched configuration keeps the
// historical behaviour, so enabling the feature cannot change existing traffic.
func TestRoundRobinIsTheDefault(t *testing.T) {
	pool := NewPool("codebuddy")
	pool.UpdateSlots(map[string]Provider{
		"codebuddy:a": &stubProvider{}, "codebuddy:b": &stubProvider{}, "codebuddy:c": &stubProvider{},
	})
	if got := pool.Strategy(); got != StrategyRoundRobin {
		t.Fatalf("default strategy = %q, want %q", got, StrategyRoundRobin)
	}
	seen := map[string]int{}
	for range 6 {
		seen[draw(t, pool, "m", "")]++
	}
	if len(seen) != 3 {
		t.Fatalf("round-robin touched %d accounts, want 3 (%v)", len(seen), seen)
	}
	for key, count := range seen {
		if count != 2 {
			t.Errorf("%s served %d of 6 requests, want 2", key, count)
		}
	}
}

// TestWeightedStrategySplitsTrafficByWeight pins the weighted draw: a 1:3 pair
// must send roughly 25%/75%, and every account must keep receiving traffic.
func TestWeightedStrategySplitsTrafficByWeight(t *testing.T) {
	pool := newStrategyPool(t, StrategyWeighted, map[string]int{
		"codebuddy:light": 100,
		"codebuddy:heavy": 300,
	})
	const draws = 400
	seen := map[string]int{}
	for range draws {
		seen[draw(t, pool, "m", "")]++
	}
	if seen["codebuddy:heavy"] != 300 || seen["codebuddy:light"] != 100 {
		t.Fatalf("weighted draw = %v, want heavy=300 light=100", seen)
	}
}

// TestWeightedStrategyInterleaves proves the draw is smooth rather than
// bursty: the heavy account must not take four requests in a row.
func TestWeightedStrategyInterleaves(t *testing.T) {
	pool := newStrategyPool(t, StrategyWeighted, map[string]int{
		"codebuddy:light": 100,
		"codebuddy:heavy": 300,
	})
	run := 0
	longest := 0
	for range 400 {
		if draw(t, pool, "m", "") == "codebuddy:heavy" {
			run++
			if run > longest {
				longest = run
			}
		} else {
			run = 0
		}
	}
	if longest > 3 {
		t.Fatalf("heavy account served %d requests in a row, want a smooth 3:1 interleave", longest)
	}
}

// TestFillFirstDrainsTheHeaviestAccount proves fill-first uses weight as an
// ordering, not as a share: the heaviest account takes everything while it is
// healthy.
func TestFillFirstDrainsTheHeaviestAccount(t *testing.T) {
	pool := newStrategyPool(t, StrategyFillFirst, map[string]int{
		"codebuddy:primary":   500,
		"codebuddy:secondary": 100,
	})
	for range 50 {
		if got := draw(t, pool, "m", ""); got != "codebuddy:primary" {
			t.Fatalf("fill-first served %q, want the heaviest account to keep draining", got)
		}
	}
	// A block on the primary must move traffic to the secondary, not fail.
	pool.SetHardBlocks(map[[2]string]string{{"codebuddy:primary", "m"}: "blocked"})
	if got := draw(t, pool, "m", ""); got != "codebuddy:secondary" {
		t.Fatalf("fill-first after blocking primary served %q, want the secondary", got)
	}
}

// TestAffinityKeepsOneAccountPerTurn proves the conversation pin: the requests
// of one user turn land on the same account even under round-robin, and a new
// question is free to draw the next one.
func TestAffinityKeepsOneAccountPerTurn(t *testing.T) {
	pool := newStrategyPool(t, StrategyRoundRobin, map[string]int{
		"codebuddy:a": 100, "codebuddy:b": 100,
	})
	affinity := NewAffinity()
	affinity.Configure(true, time.Minute)
	pool.SetAffinity(affinity)

	turn := &chatwire.ChatRequest{
		Model: "m",
		Messages: []chatwire.Message{
			{Role: "system", Content: []byte(`"rules"`)},
			{Role: "user", Content: []byte(`"first question"`)},
		},
	}
	first := draw(t, pool, "m", turn.SessionKey())
	for range 5 {
		if got := draw(t, pool, "m", turn.SessionKey()); got != first {
			t.Fatalf("affinity moved the conversation from %q to %q", first, got)
		}
	}

	// The tool loop replays the same question plus assistant/tool turns; the key
	// must not change, or the pin would be lost mid-turn.
	loop := &chatwire.ChatRequest{
		Model: "m",
		Messages: []chatwire.Message{
			{Role: "system", Content: []byte(`"rules"`)},
			{Role: "user", Content: []byte(`"first question"`)},
			{Role: "assistant", Content: []byte(`"calling a tool"`)},
			{Role: "tool", Content: []byte(`"tool result"`)},
		},
	}
	if loop.SessionKey() != turn.SessionKey() {
		t.Fatal("a tool round trip changed the affinity key: the pin would break inside one turn")
	}

	// A new question is a new turn and re-draws the account.
	next := &chatwire.ChatRequest{
		Model: "m",
		Messages: []chatwire.Message{
			{Role: "system", Content: []byte(`"rules"`)},
			{Role: "user", Content: []byte(`"first question"`)},
			{Role: "assistant", Content: []byte(`"answer"`)},
			{Role: "user", Content: []byte(`"second question"`)},
		},
	}
	if next.SessionKey() == turn.SessionKey() {
		t.Fatal("a new user question kept the affinity key: the account would never be re-drawn")
	}
	if got := draw(t, pool, "m", next.SessionKey()); got == first {
		t.Fatalf("new turn reused %q; round-robin should have advanced", got)
	}
}

// TestAffinityDisabledIsIgnored proves the default is inert: no pin is recorded
// and each request draws independently.
func TestAffinityDisabledIsIgnored(t *testing.T) {
	pool := newStrategyPool(t, StrategyRoundRobin, map[string]int{
		"codebuddy:a": 100, "codebuddy:b": 100,
	})
	affinity := NewAffinity()
	pool.SetAffinity(affinity)
	session := "turn-1"
	first := draw(t, pool, "m", session)
	if got := draw(t, pool, "m", session); got == first {
		t.Fatalf("affinity pinned %q while disabled", got)
	}
	if _, ok := affinity.Pinned(session); ok {
		t.Fatal("a disabled affinity store recorded a pin")
	}
}

// TestAffinityFallsBackWhenPinnedAccountIsBlocked proves a pinned account that
// became unusable does not fail the request: the pool draws another one.
func TestAffinityFallsBackWhenPinnedAccountIsBlocked(t *testing.T) {
	pool := newStrategyPool(t, StrategyRoundRobin, map[string]int{
		"codebuddy:a": 100, "codebuddy:b": 100,
	})
	affinity := NewAffinity()
	affinity.Configure(true, time.Minute)
	pool.SetAffinity(affinity)
	pinned := draw(t, pool, "m", "turn")
	pool.SetHardBlocks(map[[2]string]string{{pinned, "m"}: "blocked"})
	if got := draw(t, pool, "m", "turn"); got == pinned {
		t.Fatalf("the blocked pinned account %q still served the request", got)
	}
}

// TestNormalizeStrategyFallsBack pins the operator-input contract.
func TestNormalizeStrategyFallsBack(t *testing.T) {
	cases := map[string]string{
		"":            StrategyRoundRobin,
		"ROUND_ROBIN": StrategyRoundRobin,
		"weighted":    StrategyWeighted,
		"fill_first":  StrategyFillFirst,
		"nonsense":    StrategyRoundRobin,
		"fill-first":  StrategyRoundRobin,
		" Weighted":   StrategyRoundRobin,
	}
	for input, want := range cases {
		if got := NormalizeStrategy(input); got != want {
			t.Errorf("NormalizeStrategy(%q) = %q, want %q", input, got, want)
		}
	}
	for _, valid := range []string{StrategyRoundRobin, StrategyWeighted, StrategyFillFirst} {
		if !ValidStrategy(valid) {
			t.Errorf("ValidStrategy(%q) = false", valid)
		}
	}
	if ValidStrategy("nonsense") {
		t.Error("ValidStrategy accepted an unknown strategy")
	}
}
