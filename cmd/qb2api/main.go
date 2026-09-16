// Command qb2api runs the Go rewrite of the qoderbuddy2api backend.
//
// One process serves both surfaces the Python build split across a Control
// Plane and a Proxy Worker: the OpenAI/Anthropic proxy, the admin console API,
// the static admin UI, and the schedulers (check-in, growth, metrics, usage
// rollup, credential rotation).
//
// It is intentionally drop-in against the existing deployment: same SQLite file,
// same credential vault key, same .env variables. See README-go.md.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/config"
	"github.com/dmego/qoderbuddy2api/internal/server"
	"github.com/dmego/qoderbuddy2api/internal/store"
	"github.com/dmego/qoderbuddy2api/internal/vault"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "fatal:", err)
		os.Exit(1)
	}
}

func run() error {
	printVersion := flag.Bool("print-version", false, "print the build version and exit")
	flag.Parse()
	if *printVersion {
		fmt.Println(server.Version)
		return nil
	}

	settings := config.Load()
	configureLogging(settings.LogLevel)

	if settings.CredentialKey == "" {
		return errors.New("QB2API_CREDENTIAL_KEY is required: without it stored credentials cannot be decrypted")
	}
	credVault, err := vault.New(settings.CredentialKey)
	if err != nil {
		return fmt.Errorf("credential vault: %w", err)
	}

	db, err := store.Open(settings.DBPath())
	if err != nil {
		return fmt.Errorf("open database: %w", err)
	}
	defer db.Close()
	if err := compactHistory(db); err != nil {
		slog.Warn("history compaction skipped", "error", err)
	}

	events := store.NewEventWriter(db, time.Duration(settings.EventFlushMillis)*time.Millisecond, settings.EventFlushMax)
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := events.Close(ctx); err != nil {
			slog.Warn("final telemetry flush failed", "error", err)
		}
	}()

	plane := server.NewProxyPlane(settings, credVault)
	webDir := resolveWebDir()
	api := server.NewAPI(settings, db, credVault, events, plane, webDir)

	startupCtx, startupCancel := context.WithTimeout(context.Background(), 60*time.Second)
	if err := plane.Reload(startupCtx, db); err != nil {
		startupCancel()
		return fmt.Errorf("load provider credentials: %w", err)
	}
	startupCancel()
	slog.Info("provider pools ready",
		"version", plane.Version(),
		"accounts", plane.SlotCount(),
		"models", len(plane.AvailableModels()))

	subsystems := server.StartSubsystems(api, settings, db, credVault, plane)
	defer subsystems.Stop()

	handler := api.Handler()
	httpServer := &http.Server{
		Addr:    settings.Addr(),
		Handler: handler,
		// No write timeout: streaming completions legitimately run for minutes.
		// Read and idle timeouts still bound a stalled client.
		ReadHeaderTimeout: 15 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		slog.Info("qb2api listening",
			"addr", settings.Addr(),
			"admin_ui", settings.AdminUIEnabled,
			"data_dir", settings.DataDir)
		if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
	}()

	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	select {
	case err := <-errCh:
		return err
	case received := <-signals:
		slog.Info("shutdown requested", "signal", received.String())
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	subsystems.Stop()
	if err := httpServer.Shutdown(shutdownCtx); err != nil {
		slog.Warn("graceful shutdown incomplete", "error", err)
	}
	plane.Close()
	if err := events.Close(shutdownCtx); err != nil {
		slog.Warn("telemetry flush failed", "error", err)
	}
	if err := db.Checkpoint(shutdownCtx); err != nil {
		slog.Warn("wal checkpoint failed", "error", err)
	}
	return nil
}

// compactHistory trims the dead per-package detail out of stored metric history.
//
// It is a one-time migration for databases written by the Python collector:
// `packages` is ~97% of the points payload bytes and the history trend only
// reads scalar totals. The snapshot table keeps its full payload.
func compactHistory(db *store.DB) error {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	changed, err := db.CompactHistoryPayloads(ctx)
	if err != nil {
		return err
	}
	if changed > 0 {
		slog.Info("compacted metric history payloads", "rows", changed)
		if err := db.Vacuum(ctx); err != nil {
			slog.Warn("post-compaction vacuum failed", "error", err)
		}
	}
	return nil
}

// resolveWebDir locates the built admin console.
//
// It checks next to the executable first (the container image layout) and then
// the repository path used in development.
func resolveWebDir() string {
	if configured := os.Getenv("QB2API_WEB_DIR"); configured != "" {
		return configured
	}
	candidates := []string{}
	if executable, err := os.Executable(); err == nil {
		candidates = append(candidates, filepath.Join(filepath.Dir(executable), "web", "dist"))
	}
	candidates = append(candidates,
		// Repository root layout: the console is built into ./web/dist.
		filepath.Join("web", "dist"),
		// Running the binary from cmd/qb2api during development.
		filepath.Join("..", "..", "web", "dist"),
	)
	for _, candidate := range candidates {
		if info, err := os.Stat(filepath.Join(candidate, "index.html")); err == nil && !info.IsDir() {
			return candidate
		}
	}
	return "web/dist"
}

// configureLogging installs the process logger.
func configureLogging(level string) {
	var parsed slog.Level
	switch strings.ToLower(strings.TrimSpace(level)) {
	case "debug":
		parsed = slog.LevelDebug
	case "warn", "warning":
		parsed = slog.LevelWarn
	case "error":
		parsed = slog.LevelError
	default:
		parsed = slog.LevelInfo
	}
	handler := slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: parsed})
	slog.SetDefault(slog.New(handler))
}
