package providers

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"sort"
	"sync"

	"github.com/dmego/qoderbuddy2api/internal/chatwire"
	"github.com/dmego/qoderbuddy2api/internal/models"
)

// RoutePolicy is the administrator's routing decision for one (model, provider)
// route: priority tiers, weight inside a tier, and an enabled flag.
type RoutePolicy struct {
	Provider string
	Priority int
	Weight   int
	Enabled  bool
}

// MaxPriority and MaxWeight bound a submitted policy, matching route_policy.py.
const (
	MaxPriority = 99
	MaxWeight   = 1000
)

// RouteScheduler is a smooth weighted round-robin across priority tiers for one
// model. It reproduces route_policy.RouteScheduler exactly, including the
// "misconfigured routes go last" fallback.
type RouteScheduler struct {
	policies map[string]RoutePolicy
	counters map[int]map[string]int
	mu       sync.Mutex
}

// NewRouteScheduler builds a scheduler, normalizing weights so a disabled route
// can never win a tier draw.
func NewRouteScheduler(policies map[string]RoutePolicy) *RouteScheduler {
	normalized := make(map[string]RoutePolicy, len(policies))
	for provider, policy := range policies {
		if !policy.Enabled {
			policy.Weight = 0
		}
		normalized[provider] = policy
	}
	return &RouteScheduler{policies: normalized, counters: map[int]map[string]int{}}
}

// Order ranks the candidate providers by tier, weighted inside each tier.
func (s *RouteScheduler) Order(candidates []string) []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	tiers := map[int][]string{}
	for _, provider := range candidates {
		policy, ok := s.policies[provider]
		if !ok {
			policy = RoutePolicy{Provider: provider, Weight: 1, Enabled: true}
		}
		if !policy.Enabled || policy.Weight <= 0 {
			continue
		}
		tiers[policy.Priority] = append(tiers[policy.Priority], provider)
	}
	priorities := make([]int, 0, len(tiers))
	for priority := range tiers {
		priorities = append(priorities, priority)
	}
	sort.Ints(priorities)
	ordered := make([]string, 0, len(candidates))
	for _, priority := range priorities {
		ordered = append(ordered, s.draw(tiers[priority], priority)...)
	}
	// Providers with no usable policy (disabled or zero weight) go last so a
	// misconfiguration degrades to "still serve" instead of "no routes".
	seen := map[string]bool{}
	for _, provider := range ordered {
		seen[provider] = true
	}
	for _, provider := range candidates {
		if !seen[provider] {
			ordered = append(ordered, provider)
		}
	}
	return ordered
}

func (s *RouteScheduler) draw(providers []string, priority int) []string {
	state, ok := s.counters[priority]
	if !ok {
		state = map[string]int{}
		s.counters[priority] = state
	}
	for _, provider := range providers {
		if _, present := state[provider]; !present {
			state[provider] = 0
		}
	}
	total := 0
	for _, provider := range providers {
		if policy, ok := s.policies[provider]; ok {
			total += policy.Weight
		}
	}
	if total <= 0 {
		return providers
	}
	for _, provider := range providers {
		if policy, ok := s.policies[provider]; ok {
			state[provider] += policy.Weight
		} else {
			state[provider]++
		}
	}
	winnerIndex := 0
	for index, provider := range providers {
		if index == 0 {
			continue
		}
		current := state[provider]
		best := state[providers[winnerIndex]]
		if current > best || (current == best && index < winnerIndex) {
			winnerIndex = index
		}
	}
	winner := providers[winnerIndex]
	state[winner] -= total
	ordered := []string{winner}
	for _, provider := range providers {
		if provider != winner {
			ordered = append(ordered, provider)
		}
	}
	return ordered
}

// Router routes one unified model id across the provider pools, applying the
// stored policies and failing over before the first downstream frame.
type Router struct {
	routes    map[string][]route
	scheduler map[string]*RouteScheduler
	catalog   map[string]models.Unified
	cursor    map[string]int
	cooldown  map[[2]string]float64
	mu        sync.Mutex
}

type route struct {
	provider string
	pool     *Pool
	upstream string
}

// NewRouter builds the router from the unified catalog and the live pools.
func NewRouter(catalog map[string]models.Unified, pools map[string]*Pool, policies map[string][]RoutePolicy) *Router {
	router := &Router{
		routes:    map[string][]route{},
		scheduler: map[string]*RouteScheduler{},
		catalog:   catalog,
		cursor:    map[string]int{},
		cooldown:  map[[2]string]float64{},
	}
	for modelID, entry := range catalog {
		routes := make([]route, 0, len(entry.Routes))
		for _, candidate := range entry.Routes {
			pool, ok := pools[candidate.Provider]
			if !ok {
				continue
			}
			routes = append(routes, route{provider: candidate.Provider, pool: pool, upstream: candidate.UpstreamID})
		}
		if len(routes) == 0 {
			continue
		}
		router.routes[modelID] = routes
		router.scheduler[modelID] = NewRouteScheduler(modelPolicies(modelID, routes, policies))
	}
	return router
}

// modelPolicies fills gaps with equal-weight defaults so every route has a
// policy, mirroring model_router._model_policies.
func modelPolicies(modelID string, routes []route, policies map[string][]RoutePolicy) map[string]RoutePolicy {
	selected := map[string]RoutePolicy{}
	if stored, ok := policies[modelID]; ok {
		for _, policy := range stored {
			for _, candidate := range routes {
				if candidate.provider == policy.Provider {
					selected[policy.Provider] = policy
				}
			}
		}
	}
	for _, candidate := range routes {
		if _, ok := selected[candidate.provider]; !ok {
			selected[candidate.provider] = RoutePolicy{Provider: candidate.provider, Weight: 1, Enabled: true}
		}
	}
	return selected
}

// AvailableModels lists catalog entries with at least one live route.
func (r *Router) AvailableModels() []models.Unified {
	out := make([]models.Unified, 0, len(r.catalog))
	for _, entry := range r.catalog {
		routes, ok := r.routes[entry.ID]
		if !ok {
			continue
		}
		available := false
		for _, candidate := range routes {
			if candidate.pool.HasAvailableSlots() {
				available = true
				break
			}
		}
		if available {
			out = append(out, entry)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

// Model returns one catalog entry.
func (r *Router) Model(modelID string) (models.Unified, bool) {
	entry, ok := r.catalog[modelID]
	return entry, ok
}

// HasRoutes reports whether the router can serve a model id at all.
func (r *Router) HasRoutes(modelID string) bool {
	return len(r.routes[modelID]) > 0
}

// Complete runs a non-streaming completion across the ordered routes.
func (r *Router) Complete(ctx context.Context, request *chatwire.ChatRequest) (map[string]any, error) {
	routes, ok := r.routes[request.Model]
	if !ok {
		return nil, fmt.Errorf("%s: %w", request.Model, UnavailableError)
	}
	original := request.Model
	defer func() { request.Model = original }()
	var lastErr error
	for _, candidate := range r.ordered(request.Model, routes) {
		if !r.usable(request.Model, candidate) {
			continue
		}
		request.Model = candidate.upstream
		result, err := candidate.pool.Complete(ctx, request)
		request.Model = original
		if err == nil {
			return result, nil
		}
		r.markFailed(request.Model, candidate)
		lastErr = err
		slog.Warn("router complete failed", "model", original, "provider", candidate.provider, "error", err)
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
	}
	if lastErr != nil {
		return nil, lastErr
	}
	return nil, fmt.Errorf("%s: %w", original, UnavailableError)
}

// Stream runs a streaming completion across the ordered routes, failing over
// only before the first downstream frame.
func (r *Router) Stream(ctx context.Context, request *chatwire.ChatRequest) (FrameStream, error) {
	routes, ok := r.routes[request.Model]
	if !ok {
		return nil, fmt.Errorf("%s: %w", request.Model, UnavailableError)
	}
	original := request.Model
	var lastErr error
	for _, candidate := range r.ordered(original, routes) {
		if !r.usable(original, candidate) {
			continue
		}
		request.Model = candidate.upstream
		stream, err := candidate.pool.Stream(ctx, request)
		request.Model = original
		if err == nil {
			return &routedStream{inner: stream, router: r, model: original, route: candidate}, nil
		}
		r.markFailed(original, candidate)
		lastErr = err
		slog.Warn("router stream failed pre-commit", "model", original, "provider", candidate.provider, "error", err)
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
	}
	if lastErr != nil {
		return nil, lastErr
	}
	return nil, fmt.Errorf("%s: %w", original, UnavailableError)
}

// routedStream propagates a post-commit failure unchanged and marks the route
// as failed when the failure happened before the first frame.
type routedStream struct {
	inner  FrameStream
	router *Router
	model  string
	route  route
	closed bool
}

func (s *routedStream) Next() ([]byte, error) {
	frame, err := s.inner.Next()
	if err == nil || errors.Is(err, io.EOF) {
		return frame, err
	}
	if IsPrecommitFailure(err) {
		s.router.markFailed(s.model, s.route)
	}
	return nil, err
}

func (s *routedStream) Close() error {
	if s.closed {
		return nil
	}
	s.closed = true
	return s.inner.Close()
}

func (r *Router) usable(modelID string, candidate route) bool {
	if !candidate.pool.HasAvailableSlots() {
		return false
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	return nowMonotonic() >= r.cooldown[[2]string{modelID, candidate.provider}]
}

func (r *Router) markFailed(modelID string, candidate route) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.cooldown[[2]string{modelID, candidate.provider}] = nowMonotonic() + cooldownSeconds
}

func (r *Router) ordered(modelID string, routes []route) []route {
	scheduler, ok := r.scheduler[modelID]
	if !ok {
		return r.rotate(modelID, routes)
	}
	providers := make([]string, 0, len(routes))
	byProvider := make(map[string]route, len(routes))
	for _, candidate := range routes {
		providers = append(providers, candidate.provider)
		byProvider[candidate.provider] = candidate
	}
	ordered := make([]route, 0, len(routes))
	for _, provider := range scheduler.Order(providers) {
		if candidate, ok := byProvider[provider]; ok {
			ordered = append(ordered, candidate)
		}
	}
	if len(ordered) > 0 {
		return ordered
	}
	return r.rotate(modelID, routes)
}

func (r *Router) rotate(modelID string, routes []route) []route {
	r.mu.Lock()
	defer r.mu.Unlock()
	start := r.cursor[modelID] % len(routes)
	r.cursor[modelID] = (start + 1) % len(routes)
	return append(append([]route{}, routes[start:]...), routes[:start]...)
}
