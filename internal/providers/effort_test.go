package providers

import (
	"encoding/json"
	"testing"

	"github.com/dmego/qoderbuddy2api/internal/chatwire"
)

// TestClientEffortReplacesProviderDefault proves the client's reasoning_effort
// reaches the upstream body untouched.
//
// reasoning_effort is a typed request field, so UnmarshalJSON keeps it out of
// Extra; while the key was only listed in passthroughKeys it fell through the
// extras branch, was dropped, and every request was silently downgraded to the
// provider default (max for the domestic pool, low for the international one).
func TestClientEffortReplacesProviderDefault(t *testing.T) {
	const wanted = "high"
	cases := []struct {
		name     string
		build    func(*chatwire.ChatRequest) map[string]any
		defaults string
	}{
		{
			name: "domestic",
			build: func(r *chatwire.ChatRequest) map[string]any {
				return NewWorkBuddy("t", "https://x", "max", 0).buildBody(r)
			},
			defaults: "max",
		},
		{
			name: "international",
			build: func(r *chatwire.ChatRequest) map[string]any {
				return NewWorkBuddyIntl("t", "https://x", "low", 0).buildBody(r)
			},
			defaults: "low",
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			request := decodeRequest(t, `{"model":"deepseek-v4.1-flash","stream":true,"reasoning_effort":"`+wanted+`",
				"messages":[{"role":"system","content":"a"},{"role":"user","content":"b"}]}`)

			body := testCase.build(request)
			if got := body["reasoning_effort"]; got != wanted {
				t.Fatalf("forwarded reasoning_effort = %v, want %q (provider default is %q)",
					got, wanted, testCase.defaults)
			}
			if request.EffectiveEffort != "" {
				t.Fatalf("effective effort = %q, want empty: the default must not be injected when the client chose one",
					request.EffectiveEffort)
			}
			if request.Telemetry()["reasoning_effort"] != wanted {
				t.Fatalf("telemetry effort = %v, want %q", request.Telemetry()["reasoning_effort"], wanted)
			}
		})
	}
}

// TestProviderDefaultEffortAppliesWhenClientSilent proves the injection path
// still works when the client sends nothing, and is what telemetry reports.
func TestProviderDefaultEffortAppliesWhenClientSilent(t *testing.T) {
	request := decodeRequest(t, `{"model":"deepseek-v4.1-flash","stream":true,
		"messages":[{"role":"system","content":"a"},{"role":"user","content":"b"}]}`)

	body := NewWorkBuddy("t", "https://x", "max", 0).buildBody(request)
	if got := body["reasoning_effort"]; got != "max" {
		t.Fatalf("forwarded reasoning_effort = %v, want the provider default max", got)
	}
	if request.EffectiveEffort != "max" {
		t.Fatalf("effective effort = %q, want max", request.EffectiveEffort)
	}
	if got := request.Telemetry()["reasoning_effort"]; got != "max" {
		t.Fatalf("telemetry effort = %v, want max", got)
	}
}

func decodeRequest(t *testing.T, raw string) *chatwire.ChatRequest {
	t.Helper()
	var request chatwire.ChatRequest
	if err := json.Unmarshal([]byte(raw), &request); err != nil {
		t.Fatalf("decode request: %v", err)
	}
	return &request
}
