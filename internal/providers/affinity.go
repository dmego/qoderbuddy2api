package providers

import (
	"sync"
	"time"
)

// Account strategies. These are operator-facing values: they travel through
// runtime settings, the admin API and the console, so the strings are part of
// the contract and a new one is added, never renamed.
const (
	// StrategyRoundRobin spreads requests evenly across the healthy accounts of
	// one provider. This is the behaviour the build has always had.
	StrategyRoundRobin = "round_robin"
	// StrategyWeighted distributes traffic by each account's configured weight
	// (smooth weighted round-robin, so a 3:1 pair interleaves instead of
	// bursting).
	StrategyWeighted = "weighted"
	// StrategyFillFirst drains one account before moving to the next. Weight
	// acts as priority here: it orders the accounts, it does not split traffic.
	StrategyFillFirst = "fill_first"
)

// DefaultAccountWeight is the weight a slot carries when nothing was stored.
// 100 (not 1) keeps hand-entered weights readable, e.g. 100/200/400.
const DefaultAccountWeight = 100

// NormalizeStrategy maps arbitrary operator input onto a known strategy,
// falling back to round-robin so an unrecognised value cannot stop routing.
func NormalizeStrategy(value string) string {
	switch value {
	case StrategyWeighted:
		return StrategyWeighted
	case StrategyFillFirst:
		return StrategyFillFirst
	default:
		return StrategyRoundRobin
	}
}

// ValidStrategy reports whether the value is one of the known strategies. It is
// what the settings validator uses, so a typo is refused instead of silently
// degrading to round-robin.
func ValidStrategy(value string) bool {
	return value == StrategyRoundRobin || value == StrategyWeighted || value == StrategyFillFirst
}

// DefaultAffinityTTL bounds how long an idle conversation stays pinned.
const DefaultAffinityTTL = time.Hour

// affinityLimit caps the number of remembered conversations. The entries are a
// few dozen bytes each and only touch memory, but an unbounded map would still
// let a long-lived proxy grow without limit under a high-cardinality client.
const affinityLimit = 4096

type affinityEntry struct {
	slotKey   string
	expiresAt time.Time
}

// Affinity remembers which account served a conversation, so the tool loop of
// one user turn keeps landing on the same upstream account.
//
// Why it matters: the upstream prompt cache is prefix based and per account, so
// a turn whose tool continuations hop between accounts pays a cold prefix on
// every hop. The pin is re-drawn whenever the client starts a new user turn
// ("前面用户再提问") and is dropped from selection when the account turns
// unusable, which leaves the pool free to fail over.
//
// Everything here is optional: with affinity disabled (the default) Lookup
// always misses and the pools behave exactly as before.
type Affinity struct {
	mu      sync.Mutex
	enabled bool
	ttl     time.Duration
	entries map[string]affinityEntry
}

// NewAffinity builds a disabled affinity store.
func NewAffinity() *Affinity {
	return &Affinity{ttl: DefaultAffinityTTL, entries: map[string]affinityEntry{}}
}

// Configure applies the operator's affinity setting. Disabling it clears the
// pins so a later re-enable cannot resurrect a stale assignment.
func (a *Affinity) Configure(enabled bool, ttl time.Duration) {
	if a == nil {
		return
	}
	if ttl <= 0 {
		ttl = DefaultAffinityTTL
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.enabled = enabled
	a.ttl = ttl
	if !enabled {
		a.entries = map[string]affinityEntry{}
	}
}

// Enabled reports whether affinity is currently active.
func (a *Affinity) Enabled() bool {
	if a == nil {
		return false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.enabled
}

// Lookup returns the pinned slot for a session, extending its lifetime: the TTL
// is idle-based, so an active conversation never loses its account mid-turn.
func (a *Affinity) Lookup(session string) (string, bool) {
	if a == nil || session == "" {
		return "", false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.enabled {
		return "", false
	}
	entry, ok := a.entries[session]
	if !ok {
		return "", false
	}
	now := time.Now()
	if !now.Before(entry.expiresAt) {
		delete(a.entries, session)
		return "", false
	}
	entry.expiresAt = now.Add(a.ttl)
	a.entries[session] = entry
	return entry.slotKey, true
}

// Pinned returns the pinned slot without extending its lifetime, used when
// checking whether the account that just failed was the pinned one.
func (a *Affinity) Pinned(session string) (string, bool) {
	if a == nil || session == "" {
		return "", false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.enabled {
		return "", false
	}
	entry, ok := a.entries[session]
	if !ok || !time.Now().Before(entry.expiresAt) {
		return "", false
	}
	return entry.slotKey, true
}

// Pin records the account serving a session. A no-op when affinity is disabled,
// so the request path never pays for a feature the operator did not turn on.
func (a *Affinity) Pin(session, slotKey string) {
	if a == nil || session == "" || slotKey == "" {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.enabled {
		return
	}
	now := time.Now()
	if len(a.entries) >= affinityLimit {
		a.evictLocked(now)
	}
	a.entries[session] = affinityEntry{slotKey: slotKey, expiresAt: now.Add(a.ttl)}
}

// Forget drops one session's pin, used when an account is retired.
func (a *Affinity) Forget(session string) {
	if a == nil || session == "" {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	delete(a.entries, session)
}

// SlotCount reports the number of live pins, for the admin service view.
func (a *Affinity) SlotCount() int {
	if a == nil {
		return 0
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.entries)
}

// evictLocked drops expired entries first and then the entries expiring
// soonest, so a full map still accepts a new pin.
func (a *Affinity) evictLocked(now time.Time) {
	for session, entry := range a.entries {
		if !now.Before(entry.expiresAt) {
			delete(a.entries, session)
		}
	}
	for len(a.entries) >= affinityLimit {
		oldestSession := ""
		var oldest time.Time
		for session, entry := range a.entries {
			if oldestSession == "" || entry.expiresAt.Before(oldest) {
				oldestSession, oldest = session, entry.expiresAt
			}
		}
		if oldestSession == "" {
			return
		}
		delete(a.entries, oldestSession)
	}
}

// providerOfSlotKey extracts the provider prefix from a pool slot key
// ("codebuddy:cb-123"). Account ids never contain a colon, and the split is on
// the first one so a future provider name with a colon still resolves.
func providerOfSlotKey(slotKey string) string {
	for index := range len(slotKey) {
		if slotKey[index] == ':' {
			return slotKey[:index]
		}
	}
	return slotKey
}
