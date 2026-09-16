package server

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/chatwire"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/providers"
	"github.com/dmego/qoderbuddy2api/internal/store"
)

// probeResult reports one live reachability check.
type probeResult struct {
	OK        bool
	Detail    string
	LatencyMS int64
}

// probeAccount runs a minimal streaming completion with the account's stored
// credential, verifying the credential still authenticates upstream.
func (a *API) probeAccount(r *http.Request, provider, accountID string) probeResult {
	record, err := a.DB.GetCredential(r.Context(), provider, accountID, "chat")
	if err != nil {
		return probeResult{OK: false, Detail: "no chat credential for this account"}
	}
	payload, err := a.Vault.Decrypt(record.EncryptedPayload)
	if err != nil {
		return probeResult{OK: false, Detail: "credential could not be decrypted"}
	}
	token := primaryToken(provider, payload)
	if token == "" {
		return probeResult{OK: false, Detail: "credential carries no bearer token"}
	}
	// The domestic gateway rejects single-turn payloads and requires a leading
	// system turn; the probe mirrors both so it exercises the real path.
	probe := &chatwire.ChatRequest{
		Model:  "hy3",
		Stream: true,
		Messages: []chatwire.Message{
			{Role: "system", Content: json.RawMessage(`"You are a helpful assistant."`)},
			{Role: "user", Content: json.RawMessage(`"ping"`)},
		},
	}
	ctx, cancel := ctxWithTimeout(r.Context(), 20*time.Second)
	defer cancel()
	started := time.Now()
	var upstream providers.Provider
	switch provider {
	case models.ProviderWorkBuddy:
		upstream = providers.NewWorkBuddy(token, a.Settings.CodeBuddyEndpoint, a.Settings.CodeBuddyDefaultReasoning,
			upstreamHeaderTimeout(a.Settings))
	case models.ProviderWorkBuddyIntl:
		upstream = providers.NewWorkBuddyIntl(token, a.Settings.WorkBuddyIntlEndpoint, a.Settings.WorkBuddyIntlReasoning,
			upstreamHeaderTimeout(a.Settings))
	default:
		return probeResult{OK: false, Detail: "unsupported provider: " + provider}
	}
	defer upstream.Close()
	stream, err := upstream.Stream(ctx, probe)
	if err != nil {
		return probeResult{OK: false, Detail: probeFailureDetail(err), LatencyMS: time.Since(started).Milliseconds()}
	}
	defer stream.Close()
	for {
		frame, err := stream.Next()
		if err != nil {
			break
		}
		if providers.FrameHasContent(frame) {
			return probeResult{OK: true, Detail: "upstream replied", LatencyMS: time.Since(started).Milliseconds()}
		}
	}
	return probeResult{OK: false, Detail: "upstream accepted the credential but produced no output", LatencyMS: time.Since(started).Milliseconds()}
}

// probeRoute probes one (provider, model) route through the pool.
func (a *API) probeRoute(r *http.Request, provider, modelID, upstreamID string) map[string]any {
	if a.Plane == nil {
		return map[string]any{"provider": provider, "status": "unavailable", "detail": "runtime starting"}
	}
	probe := &chatwire.ChatRequest{
		Model:  upstreamID,
		Stream: true,
		Messages: []chatwire.Message{
			{Role: "system", Content: json.RawMessage(`"You are a helpful assistant."`)},
			{Role: "user", Content: json.RawMessage(`"ping"`)},
		},
	}
	ctx, cancel := ctxWithTimeout(r.Context(), 30*time.Second)
	defer cancel()
	started := time.Now()
	stream, err := a.Plane.Stream(ctx, probe)
	if err != nil {
		return map[string]any{
			"provider": provider, "status": "failed", "detail": probeFailureDetail(err),
			"latency_ms": time.Since(started).Milliseconds(),
		}
	}
	defer stream.Close()
	for {
		frame, err := stream.Next()
		if err != nil {
			break
		}
		if providers.FrameHasContent(frame) {
			return map[string]any{
				"provider": provider, "status": "ok", "detail": "upstream replied",
				"latency_ms": time.Since(started).Milliseconds(),
			}
		}
	}
	return map[string]any{
		"provider": provider, "status": "failed", "detail": "no output produced",
		"latency_ms": time.Since(started).Milliseconds(),
	}
}

// probeFailureDetail renders an upstream failure without echoing the body
// verbatim (which can carry internal identifiers) beyond a bounded message.
func probeFailureDetail(err error) string {
	if err == nil {
		return ""
	}
	if errors.Is(err, providers.UnavailableError) {
		return "no available account for this provider"
	}
	return truncate(err.Error(), 300)
}

// localDate is today's date in the configured check-in timezone.
func (a *API) localDate() string {
	return store.LocalDate(a.Settings.CheckinTimezone, time.Now())
}

// ---- service lifecycle ----

// serviceName is the single supervised service in the Go build. The rewrite
// runs the proxy and the control plane in one process, so there is no separate
// worker to start or stop; the endpoints remain because the console renders
// them and operators rely on the status readout.
const serviceName = "qb2api"

func (a *API) handleServiceStatus(w http.ResponseWriter, r *http.Request) {
	runtime, err := a.DB.GetServiceRuntime(r.Context(), serviceName)
	if err != nil {
		runtime = store.ServiceRuntime{
			ServiceName:   serviceName,
			DesiredState:  "running",
			ObservedState: "HEALTHY",
			StartedAt:     ptrString(store.FormatISO(a.startedAt)),
			UpdatedAt:     store.NowISO(),
		}
	}
	written, dropped, lastErr, pending := int64(0), int64(0), error(nil), 0
	if a.Events != nil {
		written, dropped, lastErr, pending = a.Events.Stats()
	}
	inFlight := 0
	if a.Plane != nil {
		inFlight = a.Plane.InFlight()
	}
	healthError := any(nil)
	if lastErr != nil {
		healthError = lastErr.Error()
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"service_name":   runtime.ServiceName,
		"desired_state":  runtime.DesiredState,
		"observed_state": runtime.ObservedState,
		"in_flight":      inFlight,
		"runtime":        runtime,
		"telemetry": map[string]any{
			"written": written, "dropped": dropped, "pending": pending, "last_error": healthError,
		},
		"version":        Version,
		"uptime_seconds": int(time.Since(a.startedAt).Seconds()),
	})
}

// handleServiceAction accepts the lifecycle verbs the console offers. In the
// single-process build start/stop/restart would terminate the process serving
// the request, so they report the current state instead of acting on it.
func (a *API) handleServiceAction(w http.ResponseWriter, r *http.Request) {
	action := r.PathValue("action")
	switch action {
	case "start", "stop", "restart", "reload":
		operationID := "op-" + randomToken(8)
		finished := store.NowISO()
		operation := store.ServiceOperation{
			OperationID: operationID,
			ServiceName: serviceName,
			Action:      action,
			Status:      "succeeded",
			CreatedAt:   store.NowISO(),
			FinishedAt:  &finished,
		}
		_ = a.DB.PutServiceOperation(r.Context(), operation)
		detail := "single-process build: the proxy and control plane share one process"
		if action == "reload" {
			if a.Plane != nil {
				if err := a.Plane.Reload(r.Context(), a.DB); err != nil {
					operation.Status = "failed"
					message := err.Error()
					operation.Error = &message
					_ = a.DB.PutServiceOperation(r.Context(), operation)
					writeError(w, http.StatusInternalServerError, err.Error())
					return
				}
				detail = "runtime reloaded"
			}
		}
		_ = a.DB.RecordServiceEvent(r.Context(), store.ServiceEvent{
			EventID:     "se-" + randomToken(8),
			ServiceName: serviceName,
			EventType:   "action",
			Action:      &action,
			Status:      &operation.Status,
			OperationID: &operationID,
		})
		a.audit(r, "service."+action, "service", serviceName, nil)
		writeJSON(w, http.StatusOK, map[string]any{
			"status":       operation.Status,
			"operation_id": operationID,
			"action":       action,
			"detail":       detail,
		})
	default:
		writeError(w, http.StatusBadRequest, "unknown service action: "+action)
	}
}

func (a *API) handleServiceEvents(w http.ResponseWriter, r *http.Request) {
	limit, offset, err := pageFromRequest(r, 20)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	query := r.URL.Query()
	events, err := a.DB.ListServiceEvents(r.Context(), query.Get("event_type"), query.Get("status"), limit, offset)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"events":      list(events),
		"next_cursor": nextCursor(offset, limit, len(events)),
	})
}

func (a *API) handleServiceOperation(w http.ResponseWriter, r *http.Request) {
	operation, err := a.DB.GetServiceOperation(r.Context(), r.PathValue("operationID"))
	if err != nil {
		writeError(w, http.StatusNotFound, "operation not found")
		return
	}
	writeJSON(w, http.StatusOK, operation)
}

// ---- usage ----

func (a *API) registerUsage(mux *http.ServeMux, guard func(http.HandlerFunc) http.HandlerFunc) {
	mux.HandleFunc("GET /api/admin/usage/summary", guard(a.handleUsageSummary))
	mux.HandleFunc("GET /api/admin/usage/events", guard(a.handleUsageEvents))
	mux.HandleFunc("GET /api/admin/usage/events/{eventID}", guard(a.handleUsageEvent))
	mux.HandleFunc("GET /api/admin/usage/rollups", guard(a.handleUsageRollups))
	mux.HandleFunc("GET /api/admin/usage/timeseries", guard(a.handleUsageTimeseries))
	mux.HandleFunc("POST /api/admin/usage/rollup", guard(a.handleUsageRollupNow))
	mux.HandleFunc("GET /api/admin/usage/export", guard(a.handleUsageExport))
}

// usageFilterFromRequest reads the shared filter query parameters.
func usageFilterFromRequest(r *http.Request) (store.UsageFilter, error) {
	query := r.URL.Query()
	return store.UsageFilter{
		Provider:      query.Get("provider"),
		AccountID:     query.Get("account_id"),
		ModelID:       query.Get("model_id"),
		Status:        query.Get("status"),
		StartedAfter:  normalizeTimeParam(query.Get("started_after")),
		StartedBefore: normalizeTimeParam(query.Get("started_before")),
	}, nil
}

// normalizeTimeParam accepts either the stored ISO form or an RFC3339 instant
// with an offset, always returning the stored lexicographic form.
func normalizeTimeParam(value string) string {
	if value == "" {
		return ""
	}
	if parsed, ok := store.ParseISO(value); ok {
		return store.FormatISO(parsed)
	}
	return value
}

func (a *API) handleUsageSummary(w http.ResponseWriter, r *http.Request) {
	filter, err := usageFilterFromRequest(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	summary, err := a.DB.SummarizeUsage(r.Context(), filter)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"summary":     summary,
		"observed_at": store.NowISO(),
		"status":      "fresh",
	})
}

func (a *API) handleUsageEvents(w http.ResponseWriter, r *http.Request) {
	limit, offset, err := pageFromRequest(r, 25)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	filter, _ := usageFilterFromRequest(r)
	events, err := a.DB.ListRequestEvents(r.Context(), filter, limit, offset)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"events":      list(events),
		"next_cursor": nextCursor(offset, limit, len(events)),
	})
}

func (a *API) handleUsageEvent(w http.ResponseWriter, r *http.Request) {
	event, err := a.DB.GetRequestEvent(r.Context(), r.PathValue("eventID"))
	if err != nil {
		writeError(w, http.StatusNotFound, "event not found")
		return
	}
	writeJSON(w, http.StatusOK, event)
}

func (a *API) handleUsageRollups(w http.ResponseWriter, r *http.Request) {
	limit, offset, err := pageFromRequest(r, 100)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	filter, _ := usageFilterFromRequest(r)
	rollups, err := a.DB.ListRollups(r.Context(), filter, r.URL.Query().Get("bucket_kind"), limit, offset)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"rollups": list(rollups)})
}

// handleUsageTimeseries aggregates request events directly when a status filter
// is present (rollups do not carry status) and otherwise serves the stored
// buckets. The Python original had two code paths here; the Go build keeps both
// semantics but never applies the pagination cursor twice.
func (a *API) handleUsageTimeseries(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	bucketKind := query.Get("bucket_kind")
	if bucketKind == "" {
		bucketKind = "minute"
	}
	limit, _, err := pageFromRequest(r, 60)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	filter, _ := usageFilterFromRequest(r)
	if filter.Status != "" {
		rollups, err := a.DB.TimeseriesFromEvents(r.Context(), filter, bucketKind, limit)
		if err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"rollups": list(rollups)})
		return
	}
	rollups, err := a.DB.ListRollups(r.Context(), filter, bucketKind, limit, 0)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"rollups": list(rollups)})
}

func (a *API) handleUsageRollupNow(w http.ResponseWriter, r *http.Request) {
	groups, deleted, err := a.DB.RollupOnce(r.Context(), time.Now(), a.Settings.UsageDetailRetention)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status": map[string]any{"groups": groups, "deleted_events": deleted},
	})
}

// handleUsageExport streams the filtered events as CSV.
func (a *API) handleUsageExport(w http.ResponseWriter, r *http.Request) {
	filter, _ := usageFilterFromRequest(r)
	limit := 500
	if raw := r.URL.Query().Get("limit"); raw != "" {
		if parsed, ok := numeric(raw); ok {
			limit = parsed
		}
	}
	events, err := a.DB.ListRequestEvents(r.Context(), filter, limit, 0)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	w.Header().Set("Content-Type", "text/csv; charset=utf-8")
	w.Header().Set("Content-Disposition", `attachment; filename="usage-events.csv"`)
	writer := newCSVWriter(w)
	// Column order is part of the export contract the console advertises.
	_ = writer.write([]string{
		"event_id", "request_id", "provider", "account_id", "model_id", "protocol", "status",
		"http_status", "input_tokens", "output_tokens", "latency_ms", "first_token_ms",
		"stream_committed", "started_at", "finished_at", "error_code", "reasoning_effort",
	})
	for _, event := range events {
		_ = writer.write([]string{
			event.EventID, event.RequestID, event.Provider, derefString(event.AccountID),
			event.ModelID, event.Protocol, event.Status, derefInt(event.HTTPStatus),
			derefInt(event.InputTokens), derefInt(event.OutputTokens), derefInt(event.LatencyMS),
			derefInt(event.FirstTokenMS), boolText(event.StreamCommitted), event.StartedAt,
			derefString(event.FinishedAt), derefString(event.ErrorCode), derefString(event.ReasoningEffort),
		})
	}
	writer.flush()
}

// ---- metrics ----

func (a *API) registerMetrics(mux *http.ServeMux, guard func(http.HandlerFunc) http.HandlerFunc) {
	mux.HandleFunc("GET /api/admin/metrics/accounts", guard(a.handleMetricSnapshots))
	mux.HandleFunc("GET /api/admin/metrics/accounts/{provider}/{accountID}", guard(a.handleAccountMetrics))
	mux.HandleFunc("GET /api/admin/metrics/accounts/{provider}/{accountID}/history/{metricKind}", guard(a.handleMetricHistory))
	mux.HandleFunc("POST /api/admin/metrics/refresh", guard(a.handleMetricsRefresh))
	mux.HandleFunc("GET /api/admin/metrics/refresh/{operationID}", guard(a.handleMetricsRefreshStatus))
}

func (a *API) handleMetricSnapshots(w http.ResponseWriter, r *http.Request) {
	limit, _, err := pageFromRequest(r, 500)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	snapshots, err := a.DB.ListMetricSnapshots(r.Context(), r.URL.Query().Get("provider"), "")
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"snapshots": list(paginate(snapshots, 0, limit))})
}

func (a *API) handleAccountMetrics(w http.ResponseWriter, r *http.Request) {
	snapshots, err := a.DB.ListMetricSnapshots(r.Context(), r.PathValue("provider"), r.PathValue("accountID"))
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"snapshots": list(snapshots)})
}

func (a *API) handleMetricHistory(w http.ResponseWriter, r *http.Request) {
	limit := 500
	if parsed, ok := numeric(r.URL.Query().Get("limit")); ok {
		limit = parsed
	}
	since := normalizeTimeParam(r.URL.Query().Get("since"))
	rows, err := a.DB.ListMetricHistory(r.Context(), r.PathValue("provider"), r.PathValue("accountID"),
		r.PathValue("metricKind"), since, limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"rows": list(rows)})
}

func (a *API) handleMetricsRefresh(w http.ResponseWriter, r *http.Request) {
	if a.Metrics == nil {
		writeError(w, http.StatusServiceUnavailable, "metrics collector unavailable")
		return
	}
	operationID := "mr-" + randomToken(8)
	writeJSON(w, http.StatusOK, map[string]any{"operation_id": operationID, "status": "running"})
	go a.Metrics.RefreshAll(context.Background(), operationID)
}

func (a *API) handleMetricsRefreshStatus(w http.ResponseWriter, r *http.Request) {
	operation, err := a.DB.GetMetricRefreshOperation(r.Context(), r.PathValue("operationID"))
	if err != nil {
		writeError(w, http.StatusNotFound, "operation not found")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"operation_id": operation.OperationID, "status": operation.Status,
		"result": operation.Result, "error_code": operation.ErrorCode,
		"created_at": operation.CreatedAt, "finished_at": operation.FinishedAt,
	})
}

// list renders a slice so that an empty result marshals as [] rather than null.
//
// A nil Go slice serialises to JSON null, and the console dereferences these
// fields directly (`state.events.length`). A null where an array belongs makes
// the whole page throw during render and show as blank, which looks like the
// API failed even though it answered 200.
func list[T any](values []T) []T {
	if values == nil {
		return []T{}
	}
	return values
}

// ---- small pointer helpers ----

func ptrString(value string) *string { return &value }

func derefString(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func derefInt(value *int) string {
	if value == nil {
		return ""
	}
	return strconvItoa(*value)
}

func boolText(value bool) string {
	if value {
		return "true"
	}
	return "false"
}

func strconvItoa(value int) string {
	if value == 0 {
		return "0"
	}
	negative := value < 0
	if negative {
		value = -value
	}
	var buffer [20]byte
	index := len(buffer)
	for value > 0 {
		index--
		buffer[index] = byte('0' + value%10)
		value /= 10
	}
	if negative {
		index--
		buffer[index] = '-'
	}
	return string(buffer[index:])
}
