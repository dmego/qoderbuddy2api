package server

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/chatwire"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/providers"
	"github.com/dmego/qoderbuddy2api/internal/store"
)

// registerProxy installs the OpenAI/Anthropic surface and model discovery.
func (a *API) registerProxy(mux *http.ServeMux) {
	mux.HandleFunc("POST /v1/chat/completions", a.proxyAuth(a.handleChatCompletions))
	mux.HandleFunc("POST /v1/messages", a.proxyAuth(a.handleAnthropicMessages))

	discovery := []string{
		"GET /v1/models", "GET /api/v1/models", "GET /api/tags",
		"GET /v1/props", "GET /props", "GET /v1/models/{model...}",
	}
	for _, pattern := range discovery {
		mux.HandleFunc(pattern, a.proxyAuth(a.handleDiscovery))
	}
	mux.HandleFunc("POST /api/show", a.proxyAuth(a.handleShow))
}

// proxyAuth enforces the proxy key on the request path.
func (a *API) proxyAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if a.Plane == nil {
			writeJSON(w, http.StatusServiceUnavailable, map[string]any{
				"error": map[string]any{"message": "proxy worker starting", "type": "unavailable"},
			})
			return
		}
		if !a.Plane.AcceptProxyKey(bearerToken(r)) {
			writeJSON(w, http.StatusUnauthorized, map[string]any{
				"error": map[string]any{
					"message": "Invalid or missing API key",
					"type":    "auth_error",
					"code":    "unauthorized",
				},
			})
			return
		}
		next(w, r)
	}
}

func (a *API) handleDiscovery(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/props" {
		writeJSON(w, http.StatusOK, map[string]any{})
		return
	}
	available := a.Plane.AvailableModels()
	if path := r.PathValue("model"); path != "" && !strings.HasSuffix(r.URL.Path, "/models") {
		writeJSON(w, http.StatusOK, map[string]any{
			"id": path, "object": "model", "created": 0, "owned_by": "qoderbuddy2api",
		})
		return
	}
	if r.URL.Path == "/v1/props" {
		ids := make([]string, 0, len(available))
		for _, model := range available {
			ids = append(ids, model.ID)
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"provides":     []string{"openai", "anthropic"},
			"capabilities": []string{"chat", "completion", "streaming", "tool_calls", "anthropic_messages"},
			"configuration": map[string]any{
				"base_url":           "http://localhost:9999/v1",
				"anthropic_base_url": "http://localhost:9999",
				"api_key":            "optional",
			},
			"models": list(ids),
		})
		return
	}
	data := make([]map[string]any, 0, len(available))
	for _, model := range available {
		data = append(data, map[string]any{
			"id": model.ID, "object": "model", "created": 0, "owned_by": "qoderbuddy2api",
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"object": "list", "data": data})
}

func (a *API) handleShow(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"details": map[string]any{"families": []string{}}})
}

// handleChatCompletions serves POST /v1/chat/completions.
func (a *API) handleChatCompletions(w http.ResponseWriter, r *http.Request) {
	started := time.Now()
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, openAIError("failed to read request body", "invalid_request_error"))
		return
	}
	var request chatwire.ChatRequest
	if err := json.Unmarshal(body, &request); err != nil {
		writeJSON(w, http.StatusBadRequest, openAIError("Invalid JSON: "+err.Error(), "invalid_request_error"))
		return
	}
	if len(request.Messages) == 0 || request.Model == "" {
		writeJSON(w, http.StatusBadRequest, openAIError("model and messages are required", "invalid_request_error"))
		return
	}
	entry, err := a.Plane.Resolve(request.Model)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, openAIError(err.Error(), "invalid_request_error"))
		return
	}
	request.Model = entry.ID

	if request.Stream {
		a.streamChat(w, r, &request, entry, started)
		return
	}
	a.completeChat(w, r, &request, entry, started)
}

func (a *API) completeChat(w http.ResponseWriter, r *http.Request, request *chatwire.ChatRequest, entry models.Unified, started time.Time) {
	result, err := a.Plane.Complete(r.Context(), request)
	if err != nil {
		a.recordEvent(request, entry, "openai", false, started, err, 0, false)
		if errors.Is(err, providers.UnavailableError) {
			writeJSON(w, http.StatusServiceUnavailable, openAIError(err.Error(), "provider_unavailable"))
			return
		}
		writeJSON(w, providers.StatusCode(err), openAIError(err.Error(), "upstream_error"))
		return
	}
	a.recordEvent(request, entry, "openai", true, started, nil, 0, false)
	writeJSON(w, http.StatusOK, result)
}

// streamChat proxies an SSE stream, recording the first-token latency the
// moment the first content-bearing frame reaches the client.
func (a *API) streamChat(w http.ResponseWriter, r *http.Request, request *chatwire.ChatRequest, entry models.Unified, started time.Time) {
	stream, err := a.Plane.Stream(r.Context(), request)
	if err != nil {
		a.recordEvent(request, entry, "openai", false, started, err, 0, true)
		if errors.Is(err, providers.UnavailableError) {
			writeJSON(w, http.StatusServiceUnavailable, openAIError(err.Error(), "provider_unavailable"))
			return
		}
		writeJSON(w, providers.StatusCode(err), openAIError(err.Error(), "upstream_error"))
		return
	}
	defer stream.Close()

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	flusher, _ := w.(http.Flusher)

	firstToken := 0
	var streamErr error
	for {
		frame, err := stream.Next()
		if err != nil {
			if !errors.Is(err, io.EOF) {
				streamErr = err
				// A failure after the first downstream frame cannot fail over;
				// it is reported in-band the way the Python adapter did.
				request.RecordStreamError(typeName(err))
				frame = openAIErrorFrame(err.Error(), "upstream_error")
			}
		}
		if len(frame) > 0 {
			if firstToken == 0 && providers.FrameHasContent(frame) {
				firstToken = int(time.Since(started).Milliseconds())
			}
			request.ObserveStreamChunk(frame)
			if _, writeErr := w.Write(frame); writeErr != nil {
				streamErr = writeErr
				break
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if err != nil {
			break
		}
	}
	success := streamErr == nil && request.StreamError == ""
	a.recordEvent(request, entry, "openai", success, started, streamErr, firstToken, true)
}

// handleAnthropicMessages serves POST /v1/messages.
func (a *API) handleAnthropicMessages(w http.ResponseWriter, r *http.Request) {
	started := time.Now()
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeAnthropicError(w, http.StatusBadRequest, "failed to read request body")
		return
	}
	var raw map[string]any
	if err := json.Unmarshal(body, &raw); err != nil {
		writeAnthropicError(w, http.StatusBadRequest, "Invalid JSON: "+err.Error())
		return
	}
	converted, err := anthropicToOpenAI(raw)
	if err != nil {
		writeAnthropicError(w, http.StatusBadRequest, "Invalid Anthropic request: "+err.Error())
		return
	}
	encoded, err := json.Marshal(converted)
	if err != nil {
		writeAnthropicError(w, http.StatusBadRequest, "Invalid Anthropic request: "+err.Error())
		return
	}
	var request chatwire.ChatRequest
	if err := json.Unmarshal(encoded, &request); err != nil {
		writeAnthropicError(w, http.StatusBadRequest, "Invalid Anthropic request: "+err.Error())
		return
	}
	originalModel := request.Model
	entry, err := a.Plane.Resolve(originalModel)
	if err != nil {
		writeAnthropicError(w, http.StatusBadRequest, err.Error())
		return
	}
	request.Model = entry.ID

	if request.Stream {
		a.streamAnthropic(w, r, &request, entry, originalModel, started)
		return
	}
	result, err := a.Plane.Complete(r.Context(), &request)
	if err != nil {
		a.recordEvent(&request, entry, "anthropic", false, started, err, 0, false)
		writeAnthropicError(w, providers.StatusCode(err), err.Error())
		return
	}
	a.recordEvent(&request, entry, "anthropic", true, started, nil, 0, false)
	writeJSON(w, http.StatusOK, openAIToAnthropic(result, originalModel))
}

func (a *API) streamAnthropic(w http.ResponseWriter, r *http.Request, request *chatwire.ChatRequest, entry models.Unified, originalModel string, started time.Time) {
	stream, err := a.Plane.Stream(r.Context(), request)
	if err != nil {
		a.recordEvent(request, entry, "anthropic", false, started, err, 0, true)
		writeAnthropicError(w, providers.StatusCode(err), err.Error())
		return
	}
	defer stream.Close()

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	flusher, _ := w.(http.Flusher)

	converter := newAnthropicStreamConverter(originalModel)
	firstToken := 0
	var streamErr error
	for {
		frame, err := stream.Next()
		if err != nil && !errors.Is(err, io.EOF) {
			streamErr = err
			request.RecordStreamError(typeName(err))
		}
		if len(frame) > 0 {
			if firstToken == 0 && providers.FrameHasContent(frame) {
				firstToken = int(time.Since(started).Milliseconds())
			}
			for _, event := range converter.consume(frame) {
				if _, writeErr := w.Write(event); writeErr != nil {
					streamErr = writeErr
					break
				}
				if flusher != nil {
					flusher.Flush()
				}
			}
		}
		if err != nil {
			break
		}
	}
	if streamErr != nil {
		_, _ = w.Write(anthropicErrorSSE(streamErr.Error()))
	}
	success := streamErr == nil && request.StreamError == ""
	a.recordEvent(request, entry, "anthropic", success, started, streamErr, firstToken, true)
}

// recordEvent writes one telemetry row through the batching writer.
func (a *API) recordEvent(request *chatwire.ChatRequest, entry models.Unified, protocol string, success bool, started time.Time, err error, firstTokenMS int, stream bool) {
	if a.Events == nil {
		return
	}
	telemetry := request.Telemetry()
	status := "succeeded"
	httpStatus := http.StatusOK
	errorCode := any(nil)
	if !success {
		status = "failed"
		httpStatus = http.StatusOK
		if err != nil {
			httpStatus = providers.StatusCode(err)
			errorCode = typeName(err)
		} else if request.StreamError != "" {
			errorCode = request.StreamError
		}
	}
	now := store.NowISO()
	event := store.RequestEvent{
		EventID:         randomToken(16),
		RequestID:       randomToken(16),
		Provider:        orDefaultString(telemetry["provider"], "unknown"),
		ModelID:         entry.ID,
		Protocol:        protocol,
		Status:          status,
		HTTPStatus:      &httpStatus,
		StreamCommitted: request.StreamCommitted,
		StartedAt:       now,
		FinishedAt:      &now,
	}
	if account, ok := telemetry["account_id"].(string); ok && account != "" {
		event.AccountID = &account
	}
	if effort, ok := telemetry["reasoning_effort"].(string); ok && effort != "" {
		event.ReasoningEffort = &effort
	}
	if tokens, ok := telemetry["input_tokens"].(int); ok {
		event.InputTokens = &tokens
	}
	if tokens, ok := telemetry["output_tokens"].(int); ok {
		event.OutputTokens = &tokens
	}
	if code, ok := errorCode.(string); ok && code != "" {
		event.ErrorCode = &code
	}
	latency := int(time.Since(started).Milliseconds())
	event.LatencyMS = &latency
	if firstTokenMS > 0 {
		event.FirstTokenMS = &firstTokenMS
	}
	if err != nil {
		message := truncate(err.Error(), 200)
		event.RedactedError = &message
	}
	a.Events.Enqueue(event)
}

// orDefaultString renders a telemetry value, substituting a fallback when it is
// absent or empty (the Python code used `or`, which also ignores "").
func orDefaultString(value any, fallback string) string {
	if text, ok := value.(string); ok && text != "" {
		return text
	}
	return fallback
}

func typeName(err error) string {
	if err == nil {
		return ""
	}
	return fmt.Sprintf("%T", err)
}

func truncate(value string, limit int) string {
	if len(value) <= limit {
		return value
	}
	return value[:limit]
}

func openAIError(message, kind string) map[string]any {
	return map[string]any{"error": map[string]any{"message": message, "type": kind}}
}

func openAIErrorFrame(message, kind string) []byte {
	payload, _ := json.Marshal(openAIError(message, kind))
	return []byte("data: " + string(payload) + "\n\n")
}

func writeAnthropicError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]any{
		"type":  "error",
		"error": map[string]any{"type": "api_error", "message": message},
	})
}

var _ = slog.Warn
