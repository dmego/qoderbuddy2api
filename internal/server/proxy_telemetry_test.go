package server

import (
	"context"
	"testing"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/chatwire"
	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/store"
)

// TestRecordEventSeparatesClientAborts proves a request the caller abandoned is
// recorded as cancelled rather than as a 502 failure. The console's error rate
// counts failures, so mislabelling caller-side endings there inflates it.
func TestRecordEventSeparatesClientAborts(t *testing.T) {
	entry := models.Unified{ID: "deepseek-v4.1-flash"}

	cases := []struct {
		name       string
		err        error
		success    bool
		wantStatus string
		wantHTTP   int
		wantCode   string
	}{
		{"success", nil, true, "succeeded", 200, ""},
		{"client cancel", context.Canceled, false, "cancelled", 499, "client_canceled"},
		{"upstream unreachable", &opaqueError{}, false, "failed", 502, "unknown_error"},
	}

	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			// Each case gets its own database so the assertion reads exactly the
			// row this case wrote.
			api, db := newTestAPI(t)
			request := &chatwire.ChatRequest{Model: entry.ID}
			started := time.Now().Add(-3 * time.Second)
			api.recordEvent(request, entry, "openai", testCase.success, started, testCase.err, 1200, true)

			if err := api.Events.Flush(context.Background()); err != nil {
				t.Fatalf("flush: %v", err)
			}
			event := latestEvent(t, db)
			if event.Status != testCase.wantStatus {
				t.Fatalf("status = %q, want %q", event.Status, testCase.wantStatus)
			}
			if event.HTTPStatus == nil || *event.HTTPStatus != testCase.wantHTTP {
				t.Fatalf("http_status = %v, want %d", event.HTTPStatus, testCase.wantHTTP)
			}
			if testCase.wantCode == "" {
				if event.ErrorCode != nil {
					t.Fatalf("error_code = %q, want empty", *event.ErrorCode)
				}
				return
			}
			if event.ErrorCode == nil || *event.ErrorCode != testCase.wantCode {
				t.Fatalf("error_code = %v, want %q", event.ErrorCode, testCase.wantCode)
			}
		})
	}
}

// TestRecordEventStampsRequestArrival proves started_at is the instant the
// request arrived: the console buckets events by it, so a multi-second request
// must not be filed under the minute it finished in, and latency must not
// contradict its own timestamps.
func TestRecordEventStampsRequestArrival(t *testing.T) {
	api, db := newTestAPI(t)
	entry := models.Unified{ID: "deepseek-v4.1-flash"}
	started := time.Now().UTC().Add(-90 * time.Second)

	api.recordEvent(&chatwire.ChatRequest{Model: entry.ID}, entry, "openai", true, started, nil, 1500, true)
	if err := api.Events.Flush(context.Background()); err != nil {
		t.Fatalf("flush: %v", err)
	}

	event := latestEvent(t, db)
	recorded, ok := store.ParseISO(event.StartedAt)
	if !ok {
		t.Fatalf("started_at %q is not parseable", event.StartedAt)
	}
	if drift := recorded.Sub(started.UTC()); drift > time.Second || drift < -time.Second {
		t.Fatalf("started_at = %s, want ~%s (drift %s)", event.StartedAt, started.Format(store.ISOFormat), drift)
	}
	if event.FinishedAt == nil {
		t.Fatal("finished_at missing")
	}
	finished, ok := store.ParseISO(*event.FinishedAt)
	if !ok {
		t.Fatalf("finished_at %q is not parseable", *event.FinishedAt)
	}
	if !finished.After(recorded) {
		t.Fatalf("finished_at %s is not after started_at %s", *event.FinishedAt, event.StartedAt)
	}
	if event.LatencyMS == nil || *event.LatencyMS < 89000 {
		t.Fatalf("latency_ms = %v, want >= 89000 for a 90s-old request", event.LatencyMS)
	}
}

// latestEvent loads the most recent request event.
func latestEvent(t *testing.T, db *store.DB) store.RequestEvent {
	t.Helper()
	events, err := db.ListRequestEvents(context.Background(), store.UsageFilter{}, 1, 0)
	if err != nil {
		t.Fatalf("list events: %v", err)
	}
	if len(events) == 0 {
		t.Fatal("no request event recorded")
	}
	return events[0]
}

// opaqueError stands in for an unclassified transport failure.
type opaqueError struct{}

func (e *opaqueError) Error() string { return "dial tcp: connection refused" }
