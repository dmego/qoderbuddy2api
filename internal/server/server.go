// Package server wires the HTTP surface: the OpenAI/Anthropic proxy, the admin
// console API, and the static admin UI.
//
// Process boundary note: the Python original ran a Control Plane and a Proxy
// Worker as two processes communicating over an internal snapshot protocol
// (internal token, versioned JSON, loopback handshake). The Go rewrite serves
// both surfaces from one process, which removes the handshake, the snapshot
// serialization and a second interpreter. The security property that mattered —
// the request path must not be able to read SQLite, the admin key, or the
// credential master key — is preserved structurally instead: the proxy
// handlers receive only *ProxyPlane, which exposes provider pools and model
// routing and holds nothing else. Credentials are decrypted once per snapshot
// rebuild and handed to the pools as opaque bearer strings.
package server

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/checkin"
	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/growth"
	"github.com/dmego/qoderbuddy2api/internal/metrics"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/oauthflow"
	"github.com/dmego/qoderbuddy2api/internal/store"
	"github.com/dmego/qoderbuddy2api/internal/vault"
)

// API carries the collaborators every handler needs.
type API struct {
	Settings config.Settings
	DB       *store.DB
	Vault    *vault.Vault
	Events   *store.EventWriter
	Plane    *ProxyPlane
	Admin    *AdminAuth

	// Scheduled subsystems. Each is nil until its constructor runs, and every
	// handler that uses one must tolerate nil so the server can boot with a
	// feature disabled.
	Checkin          *checkin.Service
	CheckinScheduler *checkin.Scheduler
	Growth           *growth.Automation
	Metrics          *metrics.Collector
	MetricsScheduler *metrics.Scheduler
	Imports          *oauthflow.Service

	// webDir holds the built admin console (index.html + assets).
	webDir string
	// startedAt backs uptime reporting.
	startedAt time.Time
}

// NewAPI builds the API shell. plane may be nil while the runtime is starting.
func NewAPI(settings config.Settings, db *store.DB, credVault *vault.Vault, events *store.EventWriter, plane *ProxyPlane, webDir string) *API {
	return &API{
		Settings:  settings,
		DB:        db,
		Vault:     credVault,
		Events:    events,
		Plane:     plane,
		Admin:     NewAdminAuth(settings, db),
		webDir:    webDir,
		startedAt: time.Now(),
	}
}

// Handler builds the top-level HTTP handler.
func (a *API) Handler() http.Handler {
	mux := http.NewServeMux()

	// Health and version are public, matching classify_path's public_existing.
	mux.HandleFunc("GET /health", a.handleHealth)
	mux.HandleFunc("GET /version", a.handleVersion)

	// Proxy surface (OpenAI + Anthropic + discovery), guarded by the proxy key.
	a.registerProxy(mux)

	// Admin API, guarded by the admin session cookie or bearer admin key.
	a.registerAdmin(mux)

	// Admin console static assets and SPA fallback.
	a.registerStatic(mux)

	return a.logRequests(a.recoverPanic(mux))
}

func (a *API) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"status":    "ok",
		"component": "control-plane",
		"worker":    a.planeState(),
	})
}

func (a *API) handleVersion(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"version":   Version,
		"component": "control-plane",
		"runtime":   "go",
	})
}

// Version is the build version reported by /version and the admin service view.
const Version = "1.3.0"

func (a *API) planeState() string {
	if a.Plane == nil {
		return "starting"
	}
	return "healthy"
}

func (a *API) registerStatic(mux *http.ServeMux) {
	assetsDir := filepath.Join(a.webDir, "assets")
	if info, err := os.Stat(assetsDir); err == nil && info.IsDir() {
		mux.Handle("GET /admin/assets/", http.StripPrefix("/admin/assets/",
			noCache(http.FileServer(http.Dir(assetsDir)))))
	}
	shell := func(w http.ResponseWriter, r *http.Request) {
		if !a.Settings.AdminUIEnabled {
			http.Error(w, "admin UI disabled", http.StatusNotFound)
			return
		}
		index := filepath.Join(a.webDir, "index.html")
		if _, err := os.Stat(index); err != nil {
			http.Error(w, "admin UI not packaged", http.StatusNotFound)
			return
		}
		w.Header().Set("Cache-Control", "no-store, no-cache, must-revalidate")
		w.Header().Set("Pragma", "no-cache")
		http.ServeFile(w, r, index)
	}
	// The console is a single-page app: every non-asset path under /admin serves
	// the same shell. "GET /admin" covers the bare path and "{path...}" covers
	// everything below it (including the empty remainder), so registering
	// "GET /admin/" as well would be a duplicate pattern Go's ServeMux rejects.
	mux.HandleFunc("GET /admin", shell)
	mux.HandleFunc("GET /admin/{path...}", func(w http.ResponseWriter, r *http.Request) {
		if strings.HasPrefix(r.PathValue("path"), "assets/") {
			http.Error(w, "asset not found", http.StatusNotFound)
			return
		}
		shell(w, r)
	})
}

func noCache(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-cache")
		next.ServeHTTP(w, r)
	})
}

// logRequests emits one structured line per request at debug level.
func (a *API) logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		recorder := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(recorder, r)
		if a.Settings.LogLevel == "debug" || recorder.status >= 400 {
			slog.Debug("http",
				"method", r.Method, "path", r.URL.Path, "status", recorder.status,
				"duration_ms", time.Since(started).Milliseconds())
		}
	})
}

func (a *API) recoverPanic(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if recovered := recover(); recovered != nil {
				slog.Error("panic serving request", "path", r.URL.Path, "panic", recovered)
				writeError(w, http.StatusInternalServerError, "internal error")
			}
		}()
		next.ServeHTTP(w, r)
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

// Flush forwards to the wrapped writer so SSE streaming is not buffered.
func (s *statusRecorder) Flush() {
	if flusher, ok := s.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

// ---- response helpers ----

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	encoder := json.NewEncoder(w)
	encoder.SetEscapeHTML(false)
	_ = encoder.Encode(payload)
}

func writeError(w http.ResponseWriter, status int, detail string) {
	writeJSON(w, status, map[string]any{"detail": detail})
}

func writeAPISuccess(w http.ResponseWriter, payload any) {
	writeJSON(w, http.StatusOK, payload)
}

// decodeBody reads and validates a JSON object request body.
func decodeBody(r *http.Request, target any) error {
	decoder := json.NewDecoder(r.Body)
	if err := decoder.Decode(target); err != nil {
		return err
	}
	return nil
}

// ---- opaque cursor pagination ----
//
// Both pagination styles the console uses are keyed by an opaque base64url
// cursor that encodes an integer offset, matching the Python codec.

func encodeCursor(offset int) string {
	if offset <= 0 {
		return ""
	}
	return base64.RawURLEncoding.EncodeToString([]byte(strconv.Itoa(offset)))
}

func decodeCursor(cursor string) (int, error) {
	if cursor == "" {
		return 0, nil
	}
	raw, err := base64.RawURLEncoding.DecodeString(cursor)
	if err != nil {
		return 0, errors.New("invalid cursor")
	}
	offset, err := strconv.Atoi(string(raw))
	if err != nil || offset < 0 {
		return 0, errors.New("invalid cursor")
	}
	return offset, nil
}

// pageFromRequest parses the limit/offset (cursor) query parameters.
func pageFromRequest(r *http.Request, defaultLimit int) (limit, offset int, err error) {
	limit = defaultLimit
	if raw := r.URL.Query().Get("limit"); raw != "" {
		parsed, convErr := strconv.Atoi(raw)
		if convErr != nil {
			return 0, 0, errors.New("invalid limit")
		}
		limit = parsed
	}
	offset, err = decodeCursor(r.URL.Query().Get("cursor"))
	if err != nil {
		return 0, 0, err
	}
	return limit, offset, nil
}

// nextCursor renders the cursor for the page after this one, or nil when the
// page was not full (no more rows).
func nextCursor(offset, limit, returned int) any {
	if returned < limit {
		return nil
	}
	return encodeCursor(offset + returned)
}

// nowISO exposes the stored timestamp format to handlers.
func nowISO() string { return store.NowISO() }

// parseISOOrZero parses a stored timestamp, returning the zero time when invalid.
func parseISOOrZero(value string) time.Time {
	parsed, _ := store.ParseISO(value)
	return parsed
}

// ctxWithTimeout returns a context bounded by the given duration.
func ctxWithTimeout(parent context.Context, timeout time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(parent, timeout)
}

// isLoopback reports whether the request arrived over the loopback interface,
// which decides the admin cookie Secure policy under mode "auto".
func isLoopback(r *http.Request) bool {
	host := r.RemoteAddr
	if index := strings.LastIndex(host, ":"); index >= 0 {
		host = host[:index]
	}
	host = strings.Trim(host, "[]")
	return host == "127.0.0.1" || host == "::1" || host == "localhost"
}

// isHTTPS reports whether the request was served over TLS (directly or via a
// trusted reverse proxy that set X-Forwarded-Proto).
func (a *API) isHTTPS(r *http.Request) bool {
	if r.TLS != nil {
		return true
	}
	return strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https")
}

// catalogForRequest is a read-only helper the admin catalog handlers share.
func (a *API) catalogForRequest() map[string]models.Unified {
	if a.Plane == nil {
		return nil
	}
	return a.Plane.Catalog()
}
