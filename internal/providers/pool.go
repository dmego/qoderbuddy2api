package providers

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"sync"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/chatwire"
)

const (
	// cooldownSeconds is how long a slot is skipped after a non-quota failure.
	cooldownSeconds = 30.0
	// modelBlockDefault/Min/Max bound a 6004 quota block. Unparseable reset
	// messages fall back to the plain cooldown, and pathological upstream
	// values are capped at a day.
	modelBlockMax     = 24 * 3600.0
	modelBlockDefault = cooldownSeconds
)

// slot is one in-memory provider credential inside a pool.
type slot struct {
	key        string
	accountID  string
	provider   Provider
	inFlight   int
	generation int
}

// precommitFailure wraps the real error so the router can tell a pre-commit
// failure (safe to fail over) from a post-commit one (must propagate).
type precommitFailure struct{ err error }

func (f *precommitFailure) Error() string { return f.err.Error() }
func (f *precommitFailure) Unwrap() error { return f.err }

// Pool is the per-provider dynamic pool: round-robin over live slots, failover
// before the first downstream chunk, cooldown and quota-block bookkeeping.
type Pool struct {
	name string

	mu           sync.Mutex
	slots        map[string]*slot
	order        []string
	rr           int
	failed       map[string]float64
	modelBlocked map[[2]string]float64
	blockReason  map[[2]string]string
	hardBlocks   map[[2]string]string
	generation   int
}

// NewPool creates an empty pool for one provider name.
func NewPool(name string) *Pool {
	return &Pool{
		name:         name,
		slots:        map[string]*slot{},
		failed:       map[string]float64{},
		modelBlocked: map[[2]string]float64{},
		blockReason:  map[[2]string]string{},
		hardBlocks:   map[[2]string]string{},
	}
}

// Name reports the provider name.
func (p *Pool) Name() string { return p.name }

// HasAvailableSlots reports whether any credential is loaded.
func (p *Pool) HasAvailableSlots() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.slots) > 0
}

// SlotCount reports the number of loaded credentials.
func (p *Pool) SlotCount() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.slots)
}

// UpdateSlots reconciles the live credential set. Slots whose provider instance
// is unchanged are reused so in-flight requests and HTTP connection pools
// survive a snapshot reload.
func (p *Pool) UpdateSlots(next map[string]Provider) {
	p.mu.Lock()
	replaced := make([]Provider, 0)
	newSlots := make(map[string]*slot, len(next))
	for key, provider := range next {
		if existing, ok := p.slots[key]; ok && existing.provider == provider {
			newSlots[key] = existing
			continue
		}
		if existing, ok := p.slots[key]; ok && existing.inFlight == 0 {
			replaced = append(replaced, existing.provider)
		}
		p.generation++
		newSlots[key] = &slot{key: key, accountID: accountIDFromKey(key), provider: provider, generation: p.generation}
	}
	for key, existing := range p.slots {
		if kept, ok := newSlots[key]; ok && kept == existing {
			continue
		}
		if _, ok := newSlots[key]; ok {
			continue
		}
		if existing.inFlight == 0 {
			replaced = append(replaced, existing.provider)
		}
	}
	p.slots = newSlots
	p.order = p.order[:0]
	for key := range next {
		p.order = append(p.order, key)
	}
	sortStrings(p.order)
	live := make(map[string]bool, len(newSlots))
	for key := range newSlots {
		live[key] = true
	}
	for key := range p.failed {
		if !live[key] {
			delete(p.failed, key)
		}
	}
	for block := range p.modelBlocked {
		if !live[block[0]] {
			delete(p.modelBlocked, block)
			delete(p.blockReason, block)
		}
	}
	for block := range p.hardBlocks {
		if !live[block[0]] {
			delete(p.hardBlocks, block)
		}
	}
	if len(p.order) > 0 {
		p.rr %= len(p.order)
	} else {
		p.rr = 0
	}
	p.mu.Unlock()

	for _, provider := range replaced {
		go func(target Provider) {
			if err := target.Close(); err != nil {
				slog.Warn("close retired slot", "pool", p.name, "error", err)
			}
		}(provider)
	}
}

// SetHardBlocks replaces the administrator's manual (slot, model) exclusions.
// Hard blocks never expire and are never canaried.
func (p *Pool) SetHardBlocks(blocks map[[2]string]string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.hardBlocks = make(map[[2]string]string, len(blocks))
	for key, reason := range blocks {
		p.hardBlocks[key] = reason
	}
}

// QuotaBlock is one active model-scoped exclusion, for observability.
type QuotaBlock struct {
	Provider     string `json:"provider"`
	AccountID    string `json:"account_id"`
	ModelID      string `json:"model_id"`
	BlockedUntil string `json:"blocked_until"`
	Reason       string `json:"reason"`
	Source       string `json:"source"`
}

// QuotaBlocks snapshots the active auto and manual model blocks.
func (p *Pool) QuotaBlocks() []QuotaBlock {
	p.mu.Lock()
	defer p.mu.Unlock()
	nowMono := nowMonotonic()
	nowWall := time.Now().UTC()
	blocks := make([]QuotaBlock, 0, len(p.modelBlocked)+len(p.hardBlocks))
	for key, until := range p.modelBlocked {
		if until <= nowMono {
			continue
		}
		blocks = append(blocks, QuotaBlock{
			Provider:     p.name,
			AccountID:    accountIDFromKey(key[0]),
			ModelID:      key[1],
			BlockedUntil: nowWall.Add(time.Duration(until-nowMono) * time.Second).Format("2006-01-02T15:04:05Z"),
			Reason:       p.blockReason[key],
			Source:       "auto",
		})
	}
	for key, reason := range p.hardBlocks {
		blocks = append(blocks, QuotaBlock{
			Provider:  p.name,
			AccountID: accountIDFromKey(key[0]),
			ModelID:   key[1],
			Reason:    reason,
			Source:    "manual",
		})
	}
	return blocks
}

// markModelBlocked records a quota block until the upstream reset instant.
func (p *Pool) markModelBlocked(key, model string, resetAt time.Time, reason string) {
	nowMono := nowMonotonic()
	delay := resetAt.Sub(time.Now())
	seconds := delay.Seconds()
	if seconds < modelBlockDefault {
		seconds = modelBlockDefault
	}
	if seconds > modelBlockMax {
		seconds = modelBlockMax
	}
	block := [2]string{key, model}
	p.modelBlocked[block] = nowMono + seconds
	if reason != "" {
		p.blockReason[block] = reason
	}
	// Opportunistically drop expired entries so the map cannot grow without
	// bound on a long-running process.
	if len(p.modelBlocked) > 1 {
		for entry, until := range p.modelBlocked {
			if until <= nowMono && entry != block {
				delete(p.modelBlocked, entry)
				delete(p.blockReason, entry)
			}
		}
	}
}

func (p *Pool) markFailed(key string, generation int) {
	existing, ok := p.slots[key]
	if !ok || existing.generation != generation {
		return
	}
	p.failed[key] = nowMonotonic() + cooldownSeconds
}

// acquire picks the next usable slot, advancing the round-robin cursor.
func (p *Pool) acquire(tried map[string]bool, model string, advance bool) *slot {
	p.mu.Lock()
	defer p.mu.Unlock()
	order := make([]string, 0, len(p.order))
	for _, key := range p.order {
		if _, ok := p.slots[key]; ok {
			order = append(order, key)
		}
	}
	if len(order) == 0 {
		return nil
	}
	count := len(order)
	start := p.rr % count
	if advance {
		p.rr = (p.rr + 1) % count
	}
	rotated := append(append([]string{}, order[start:]...), order[:start]...)
	nowMono := nowMonotonic()

	candidates := make([]string, 0, len(rotated))
	for _, key := range rotated {
		if _, blocked := p.hardBlocks[[2]string{key, model}]; blocked {
			continue
		}
		candidates = append(candidates, key)
	}
	for _, key := range candidates {
		if tried[key] {
			continue
		}
		if p.failed[key] > nowMono {
			continue
		}
		if until, blocked := p.modelBlocked[[2]string{key, model}]; blocked && until > nowMono {
			continue
		}
		handle := p.slots[key]
		handle.inFlight++
		return handle
	}
	// Everything is cooling or auto-blocked for this model: fall back to the
	// remaining candidates so a recovered account gets canaried, earliest reset
	// first (most likely to succeed).
	sortByReset(candidates, p.modelBlocked, p.failed, model, nowMono)
	for _, key := range candidates {
		if tried[key] {
			continue
		}
		handle := p.slots[key]
		handle.inFlight++
		return handle
	}
	return nil
}

func (p *Pool) release(handle *slot) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if handle.inFlight > 0 {
		handle.inFlight--
	}
}

// markOutcome records a failed attempt as a quota block or a generic cooldown.
func (p *Pool) markOutcome(handle *slot, model string, err error) {
	if resetAt := ResetAt(err); resetAt != nil && model != "" {
		p.mu.Lock()
		defer p.mu.Unlock()
		p.markModelBlocked(handle.key, model, *resetAt, err.Error())
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	p.markFailed(handle.key, handle.generation)
}

// Complete runs a non-streaming completion through the pool with failover.
func (p *Pool) Complete(ctx context.Context, request *chatwire.ChatRequest) (map[string]any, error) {
	tried := map[string]bool{}
	advance := true
	var lastErr error
	for {
		handle := p.acquire(tried, request.Model, advance)
		advance = false
		if handle == nil {
			break
		}
		original := request.Model
		result, err := func() (map[string]any, error) {
			defer p.release(handle)
			request.RecordProvider(p.name)
			request.RecordSlot(handle.key, false)
			return handle.provider.Complete(ctx, request)
		}()
		request.Model = original
		if err == nil {
			return result, nil
		}
		tried[handle.key] = true
		p.markOutcome(handle, request.Model, err)
		lastErr = err
		slog.Warn("pool complete failed", "pool", p.name, "account", handle.accountID, "error", err)
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
	}
	if lastErr != nil {
		return nil, lastErr
	}
	return nil, fmt.Errorf("%s: %w", p.name, UnavailableError)
}

// Stream runs a streaming completion through the pool with pre-commit failover.
func (p *Pool) Stream(ctx context.Context, request *chatwire.ChatRequest) (FrameStream, error) {
	tried := map[string]bool{}
	advance := true
	var lastErr error
	for {
		handle := p.acquire(tried, request.Model, advance)
		advance = false
		if handle == nil {
			break
		}
		original := request.Model
		stream, err := p.openStream(ctx, handle, request)
		request.Model = original
		if err == nil {
			return stream, nil
		}
		tried[handle.key] = true
		p.markOutcome(handle, request.Model, err)
		lastErr = err
		slog.Warn("pool stream failed pre-commit", "pool", p.name, "account", handle.accountID, "error", err)
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
	}
	if lastErr != nil {
		return nil, lastErr
	}
	return nil, fmt.Errorf("%s: %w", p.name, UnavailableError)
}

// openStream wraps the provider stream so the slot lease is released and a
// failure before the first downstream frame is reported as pre-commit (and is
// therefore safe to fail over).
func (p *Pool) openStream(ctx context.Context, handle *slot, request *chatwire.ChatRequest) (FrameStream, error) {
	request.RecordProvider(p.name)
	request.RecordSlot(handle.key, false)
	inner, err := handle.provider.Stream(ctx, request)
	if err != nil {
		p.release(handle)
		return nil, err
	}
	return &poolStream{
		inner:   inner,
		pool:    p,
		handle:  handle,
		request: request,
	}, nil
}

// poolStream releases the account lease when the stream ends and keeps the
// pre-commit/post-commit distinction the router depends on.
type poolStream struct {
	inner     FrameStream
	pool      *Pool
	handle    *slot
	request   *chatwire.ChatRequest
	released  bool
	committed bool
}

func (s *poolStream) Next() ([]byte, error) {
	frame, err := s.inner.Next()
	if err != nil {
		s.finish()
		if errors.Is(err, io.EOF) {
			return nil, io.EOF
		}
		if !s.committed {
			return nil, &precommitFailure{err: err}
		}
		return nil, err
	}
	if !s.committed {
		s.committed = true
		s.request.RecordSlot(s.handle.key, true)
	}
	return frame, nil
}

func (s *poolStream) Close() error {
	err := s.inner.Close()
	s.finish()
	return err
}

func (s *poolStream) finish() {
	if s.released {
		return
	}
	s.released = true
	s.pool.release(s.handle)
}

// IsPrecommitFailure reports whether a stream failure happened before the first
// downstream frame, i.e. whether failing over is safe.
func IsPrecommitFailure(err error) bool {
	var failure *precommitFailure
	return errors.As(err, &failure)
}

func accountIDFromKey(key string) string {
	for index := len(key) - 1; index >= 0; index-- {
		if key[index] == ':' {
			return key[index+1:]
		}
	}
	return key
}

func sortStrings(values []string) {
	for i := 1; i < len(values); i++ {
		for j := i; j > 0 && values[j] < values[j-1]; j-- {
			values[j], values[j-1] = values[j-1], values[j]
		}
	}
}

func sortByReset(keys []string, blocked map[[2]string]float64, failed map[string]float64, model string, now float64) {
	for i := 1; i < len(keys); i++ {
		for j := i; j > 0; j-- {
			left := blocked[[2]string{keys[j], model}]
			right := blocked[[2]string{keys[j-1], model}]
			if left == right {
				if failed[keys[j]] >= failed[keys[j-1]] {
					break
				}
			} else if left >= right {
				break
			}
			keys[j], keys[j-1] = keys[j-1], keys[j]
		}
	}
}
