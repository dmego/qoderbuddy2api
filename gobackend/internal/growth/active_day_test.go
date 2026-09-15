package growth

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeACP stands in for the CodeBuddy console during an active-day turn. It
// records the exact call sequence so the protocol can be asserted without
// spending a real chat turn.
type fakeACP struct {
	mu       sync.Mutex
	calls    []string
	rpc      []map[string]any
	deleted  int
	accepted bool
	// endTurn decides whether the SSE channel ever sends the terminal
	// notification; when false the turn must time out instead of succeeding.
	endTurn bool
}

func (f *fakeACP) record(entry string) {
	f.mu.Lock()
	f.calls = append(f.calls, entry)
	f.mu.Unlock()
}

func (f *fakeACP) sequence() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...)
}

func (f *fakeACP) rpcMessages() []map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]map[string]any(nil), f.rpc...)
}

// serve routes the conversation bootstrap, the session lookup and the ACP
// channel. The ACP link points back at this same server.
func (f *fakeACP) serve(t *testing.T) *httptest.Server {
	t.Helper()
	var server *httptest.Server
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/console/as/conversations/":
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body["model"] != acpModel {
				t.Errorf("conversation model = %v, want %s", body["model"], acpModel)
			}
			f.record("create_conversation")
			writeACP(w, map[string]any{"code": 0, "data": map[string]any{"id": "conv-1"}})

		case r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/session"):
			f.record("session_info")
			writeACP(w, map[string]any{"code": 0, "data": map[string]any{
				"link": server.URL + "/acp/conv-1", "token": "session-token", "cwd": "/workspace",
			}})

		case r.Method == http.MethodGet && r.URL.Path == "/acp/conv-1":
			if r.Header.Get("Acp-Connection-Id") != "" {
				t.Errorf("the opening SSE request must not carry Acp-Connection-Id")
			}
			f.record("sse_open")
			w.Header().Set("Content-Type", "text/event-stream")
			w.Header().Set("Acp-Connection-Id", "conn-1")
			w.Header().Set("acp-session-token", "acp-token")
			w.WriteHeader(http.StatusOK)
			flusher, _ := w.(http.Flusher)
			if flusher != nil {
				flusher.Flush()
			}
			if !f.endTurn {
				// Hold the channel open with no terminal notification so the
				// client must time out rather than report a completed turn.
				<-r.Context().Done()
				return
			}
			// A comment frame first: the parser must ignore non-data lines.
			fmt.Fprint(w, ": keep-alive\n\n")
			if flusher != nil {
				flusher.Flush()
			}
			time.Sleep(50 * time.Millisecond)
			notification, _ := json.Marshal(map[string]any{
				"jsonrpc": "2.0",
				"method":  "session/endTurn",
				"params":  map[string]any{"sessionId": "sess-1"},
			})
			fmt.Fprintf(w, "data: %s\n\n", notification)
			if flusher != nil {
				flusher.Flush()
			}
			<-r.Context().Done()

		case r.Method == http.MethodPost && r.URL.Path == "/acp/conv-1":
			if r.Header.Get("Acp-Connection-Id") != "conn-1" {
				t.Errorf("RPC missing Acp-Connection-Id, got %q", r.Header.Get("Acp-Connection-Id"))
			}
			if r.Header.Get("acp-session-token") != "acp-token" {
				t.Errorf("RPC missing acp-session-token, got %q", r.Header.Get("acp-session-token"))
			}
			f.mu.Lock()
			f.accepted = true
			f.mu.Unlock()
			var message map[string]any
			_ = json.NewDecoder(r.Body).Decode(&message)
			method, _ := message["method"].(string)
			id, _ := message["id"].(float64)
			f.mu.Lock()
			f.rpc = append(f.rpc, message)
			f.calls = append(f.calls, "rpc:"+method)
			f.mu.Unlock()

			// The server answers every RPC with 202 Accepted and no body: the
			// only completion signal is the SSE end-turn notification.
			if method == "session/new" {
				writeACPRaw(w, http.StatusAccepted, map[string]any{
					"jsonrpc": "2.0", "id": id, "result": map[string]any{"sessionId": "sess-1"},
				})
				return
			}
			writeACPRaw(w, http.StatusAccepted, map[string]any{
				"jsonrpc": "2.0", "id": id, "result": map[string]any{},
			})

		case r.Method == http.MethodDelete && r.URL.Path == "/acp/conv-1":
			f.mu.Lock()
			f.deleted++
			f.mu.Unlock()
			f.record("delete_conversation")
			w.WriteHeader(http.StatusOK)

		default:
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(server.Close)
	return server
}

func writeACP(w http.ResponseWriter, payload map[string]any) { writeACPRaw(w, http.StatusOK, payload) }

func writeACPRaw(w http.ResponseWriter, status int, payload map[string]any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}

// The full ACP sequence must run in order and only the end-turn notification
// completes it.
func TestActiveDayProtocolSequence(t *testing.T) {
	fake := &fakeACP{endTurn: true}
	server := fake.serve(t)
	client := NewActiveDayClient(server.URL, 2*time.Second)
	if err := client.Run(context.Background(), "access-token", "你好"); err != nil {
		t.Fatalf("run: %v", err)
	}
	want := []string{
		"create_conversation",
		"session_info",
		"sse_open",
		"rpc:initialize",
		"rpc:session/new",
		"rpc:session/set_model",
		"rpc:session/prompt",
		"delete_conversation",
	}
	got := fake.sequence()
	if len(got) != len(want) {
		t.Fatalf("sequence = %v, want %v", got, want)
	}
	for index := range want {
		if got[index] != want[index] {
			t.Fatalf("sequence[%d] = %q, want %q (full %v)", index, got[index], want[index], got)
		}
	}
	if fake.deleted != 1 {
		t.Fatalf("delete calls = %d, want 1", fake.deleted)
	}

	// The prompt must carry the rotated text and the fixed model must be set.
	messages := fake.rpcMessages()
	setModel := messages[2]
	params := setModel["params"].(map[string]any)
	if params["modelId"] != acpModel {
		t.Fatalf("modelId = %v, want %s", params["modelId"], acpModel)
	}
	if params["sessionId"] != "sess-1" {
		t.Fatalf("set_model sessionId = %v, want sess-1", params["sessionId"])
	}
	prompt := messages[3]["params"].(map[string]any)
	if prompt["sessionId"] != "sess-1" {
		t.Fatalf("prompt sessionId = %v, want sess-1", prompt["sessionId"])
	}
	blocks := prompt["prompt"].([]any)
	if len(blocks) != 1 || blocks[0].(map[string]any)["text"] != "你好" {
		t.Fatalf("prompt blocks = %#v, want one text block carrying the prompt", blocks)
	}
	init := messages[0]["params"].(map[string]any)
	if init["protocolVersion"] != float64(1) {
		t.Fatalf("protocolVersion = %v, want 1", init["protocolVersion"])
	}
	pending := messages[1]["params"].(map[string]any)
	if pending["cwd"] != "/workspace" {
		t.Fatalf("session/new cwd = %v, want /workspace", pending["cwd"])
	}
}

// A 202 with no end-turn notification is not completion: the turn must fail
// with a timeout instead of reporting a lit day.
func TestActiveDayRequiresEndTurnNotification(t *testing.T) {
	fake := &fakeACP{endTurn: false}
	server := fake.serve(t)
	client := NewActiveDayClient(server.URL, 700*time.Millisecond)
	started := time.Now()
	err := client.Run(context.Background(), "access-token", "你好")
	if err == nil {
		t.Fatal("want an error when the end-turn notification never arrives")
	}
	if code := ErrorCode(err); code != "rpc_timeout" {
		t.Fatalf("error code = %q, want rpc_timeout", code)
	}
	if elapsed := time.Since(started); elapsed > 5*time.Second {
		t.Fatalf("turn took %s, want it bounded by the configured timeout", elapsed)
	}
	// Cleanup must still delete the conversation, even on the failure path.
	if fake.deleted != 1 {
		t.Fatalf("delete calls = %d, want 1 (cleanup runs on failure)", fake.deleted)
	}
}

// The SSE parser must read data lines only, and the end-turn detector must
// accept every shape the upstream uses.
func TestParseSSEAndEndTurn(t *testing.T) {
	payload := parseSSEPayload([]string{
		": comment",
		"event: message",
		`data: {"jsonrpc":"2.0","id":1,"result":{"sessionId":"s1"}}`,
	})
	if payload["id"] != float64(1) {
		t.Fatalf("payload = %#v, want the decoded data line", payload)
	}
	if parsed := parseSSEPayload([]string{"event: ping"}); parsed != nil {
		t.Fatalf("payload without data = %#v, want nil", parsed)
	}
	if parsed := parseSSEPayload([]string{"data: not json"}); parsed != nil {
		t.Fatalf("invalid JSON = %#v, want nil", parsed)
	}
	cases := []struct {
		name    string
		message map[string]any
		want    bool
	}{
		{"method snake", map[string]any{"method": "session_end_turn"}, true},
		{"method slash", map[string]any{"method": "session/endTurn"}, true},
		{"params sessionUpdate", map[string]any{"params": map[string]any{"sessionUpdate": "session/endTurn"}}, true},
		{"nested update", map[string]any{"params": map[string]any{"update": map[string]any{"sessionUpdate": "session_end_turn"}}}, true},
		{"unrelated update", map[string]any{"params": map[string]any{"update": map[string]any{"sessionUpdate": "agent_message_chunk"}}}, false},
		{"no fields", map[string]any{}, false},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			if got := isEndTurn(testCase.message); got != testCase.want {
				t.Fatalf("isEndTurn = %v, want %v", got, testCase.want)
			}
		})
	}
}

// The growth client must send the browser fingerprint headers: without them the
// gateway answers 401.
func TestGrowthClientSendsFingerprintHeaders(t *testing.T) {
	var seen http.Header
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.Header.Clone()
		_ = json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": map[string]any{}})
	}))
	defer server.Close()

	// The base carries a path to prove Origin drops it.
	client := NewClient(server.URL+"/some/prefix", 0)
	if _, err := client.get(context.Background(), "/v2/activity/growth/profile", client.headers("tok")); err != nil {
		t.Fatalf("get: %v", err)
	}
	if got := seen.Get("Origin"); got != server.URL {
		t.Fatalf("Origin = %q, want %q (scheme+host only)", got, server.URL)
	}
	if got := seen.Get("Referer"); got != server.URL+"/profile/growth-center" {
		t.Fatalf("Referer = %q", got)
	}
	if got := seen.Get("Authorization"); got != "Bearer tok" {
		t.Fatalf("Authorization = %q", got)
	}
	if !strings.HasPrefix(seen.Get("User-Agent"), "Mozilla/5.0 (Macintosh") {
		t.Fatalf("User-Agent = %q, want a Chrome desktop fingerprint", seen.Get("User-Agent"))
	}
}

// The envelope rules decide whether a step sees data or an error code.
func TestGrowthEnvelopeHandling(t *testing.T) {
	cases := []struct {
		name    string
		status  int
		body    string
		wantErr string
	}{
		{name: "unauthorized", status: 401, body: `{}`, wantErr: "auth_rejected"},
		{name: "server error", status: 500, body: `{}`, wantErr: "http:500"},
		{name: "non-zero code", status: 200, body: `{"code":5,"data":{}}`, wantErr: "upstream_error"},
		{name: "string zero code", status: 200, body: `{"code":"0","data":{"state":"traveling"}}`},
		{name: "data not an object", status: 200, body: `{"code":0,"data":[]}`},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(testCase.status)
				_, _ = w.Write([]byte(testCase.body))
			}))
			defer server.Close()
			client := NewClient(server.URL, 0)
			data, err := client.get(context.Background(), "/x", client.headers("tok"))
			if testCase.wantErr == "" {
				if err != nil {
					t.Fatalf("err = %v, want nil", err)
				}
				if data == nil {
					t.Fatal("data = nil, want an empty map")
				}
				return
			}
			if err == nil {
				t.Fatalf("err = nil, want %s", testCase.wantErr)
			}
			if code := unavailableCode(err); code != testCase.wantErr {
				t.Fatalf("code = %q, want %q", code, testCase.wantErr)
			}
		})
	}
}
