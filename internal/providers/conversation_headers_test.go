package providers

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/dmego/qoderbuddy2api/internal/chatwire"
)

// TestOutboundConversationHeaders pins the conversation-header contract the
// upstream gateway started enforcing on 2026-10-16.
//
// Measured on the international deployment: three of four accounts answer
// 400 code=11128 "conversation id missing" unless all three headers are
// present together (X-Conversation-ID, X-Conversation-Request-ID,
// X-Conversation-Message-ID). Any single header or pair is still rejected, and
// a body conversation_id does not substitute for the headers. One account in
// the fleet still accepts requests without them, so the failure surfaced as
// per-account pool failover noise rather than a clean outage. The domestic
// deployment accepted the same triple without complaint, so both providers
// send it.
func TestOutboundConversationHeaders(t *testing.T) {
	cases := []struct {
		name  string
		build func(token, endpoint string) upstreamStreamer
	}{
		{
			name: "domestic",
			build: func(token, endpoint string) upstreamStreamer {
				return NewWorkBuddy(token, endpoint, "", 0)
			},
		},
		{
			name: "international",
			build: func(token, endpoint string) upstreamStreamer {
				return NewWorkBuddyIntl(token, endpoint, "", 0)
			},
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			captured := make(chan http.Header, 1)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				captured <- r.Header.Clone()
				w.Header().Set("Content-Type", "text/event-stream")
				_, _ = w.Write([]byte("data: [DONE]\n\n"))
			}))
			defer server.Close()

			provider := testCase.build("token", server.URL)
			defer provider.Close()
			stream, err := provider.Stream(context.Background(), conversationProbeRequest())
			if err != nil {
				t.Fatalf("Stream: %v", err)
			}
			_ = stream.Close()

			headers := <-captured
			requestID := headers.Get("X-Request-ID")
			if requestID == "" {
				t.Fatal("X-Request-ID missing")
			}
			for _, name := range []string{"X-Conversation-ID", "X-Conversation-Request-ID", "X-Conversation-Message-ID"} {
				if headers.Get(name) == "" {
					t.Errorf("%s missing: upstream answers 400/11128 without the full triple", name)
				}
			}
			if got := headers.Get("X-Conversation-Request-ID"); got != requestID {
				t.Errorf("X-Conversation-Request-ID = %q, want it to equal X-Request-ID %q", got, requestID)
			}
		})
	}
}

// upstreamStreamer is the slice of a provider this test drives.
type upstreamStreamer interface {
	Stream(ctx context.Context, request *chatwire.ChatRequest) (FrameStream, error)
	Close() error
}

func conversationProbeRequest() *chatwire.ChatRequest {
	return &chatwire.ChatRequest{
		Model:  "deepseek-v4.1-flash",
		Stream: true,
		Messages: []chatwire.Message{
			{Role: "system", Content: []byte(`"You are a helpful assistant."`)},
			{Role: "user", Content: []byte(`"hi"`)},
		},
	}
}
