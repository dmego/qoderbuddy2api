package server

import (
	"encoding/json"
	"strings"
	"testing"
)

// frame builds one OpenAI SSE frame carrying a delta.
func frame(t *testing.T, delta map[string]any, finishReason string) []byte {
	t.Helper()
	choice := map[string]any{"index": 0, "delta": delta}
	if finishReason != "" {
		choice["finish_reason"] = finishReason
	}
	payload, err := json.Marshal(map[string]any{"choices": []any{choice}})
	if err != nil {
		t.Fatalf("marshal frame: %v", err)
	}
	return []byte("data: " + string(payload) + "\n\n")
}

// events splits converter output into (event name, payload) pairs.
func events(t *testing.T, frames [][]byte) []struct {
	Name    string
	Payload map[string]any
} {
	t.Helper()
	var out []struct {
		Name    string
		Payload map[string]any
	}
	for _, frame := range frames {
		text := string(frame)
		name := ""
		payload := map[string]any{}
		for _, line := range strings.Split(strings.TrimSpace(text), "\n") {
			switch {
			case strings.HasPrefix(line, "event: "):
				name = strings.TrimPrefix(line, "event: ")
			case strings.HasPrefix(line, "data: "):
				if err := json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &payload); err != nil {
					t.Fatalf("bad data line %q: %v", line, err)
				}
			}
		}
		out = append(out, struct {
			Name    string
			Payload map[string]any
		}{Name: name, Payload: payload})
	}
	return out
}

func eventNames(values []struct {
	Name    string
	Payload map[string]any
}) []string {
	out := make([]string, 0, len(values))
	for _, value := range values {
		out = append(out, value.Name)
	}
	return out
}

// The Anthropic contract requires message_start before any content event, each
// block opened exactly once, and every block closed before message_stop. A
// converter that closes a block when the next one opens (or that emits a stop
// for a block that was never opened) produces a stream Anthropic clients reject.
func TestAnthropicStreamConverterBlockLifecycle(t *testing.T) {
	converter := newAnthropicStreamConverter("deepseek-v4.1-flash")

	var all [][]byte
	all = append(all, converter.consume(frame(t, map[string]any{"reasoning_content": "thinking..."}, ""))...)
	all = append(all, converter.consume(frame(t, map[string]any{"reasoning_content": "more"}, ""))...)
	all = append(all, converter.consume(frame(t, map[string]any{"content": "Hello"}, ""))...)
	all = append(all, converter.consume(frame(t, map[string]any{"content": " world"}, ""))...)
	all = append(all, converter.consume(frame(t, map[string]any{}, "stop"))...)

	names := eventNames(events(t, all))
	if names[0] != "message_start" {
		t.Fatalf("first event must be message_start, got %q", names[0])
	}
	if names[len(names)-1] != "message_stop" {
		t.Fatalf("last event must be message_stop, got %q", names[len(names)-1])
	}

	counts := map[string]int{}
	for _, name := range names {
		counts[name]++
	}
	// One thinking block and one text block: two starts, two stops.
	if counts["content_block_start"] != 2 {
		t.Fatalf("expected 2 content_block_start (thinking + text), got %d (%v)",
			counts["content_block_start"], names)
	}
	if counts["content_block_stop"] != 2 {
		t.Fatalf("expected 2 content_block_stop, got %d (%v)", counts["content_block_stop"], names)
	}
	if counts["message_start"] != 1 || counts["message_delta"] != 1 || counts["message_stop"] != 1 {
		t.Fatalf("terminal events must each appear once: %v", names)
	}
	// Blocks must be contiguous: no start after the first stop.
	firstStop := indexOf(names, "content_block_stop")
	lastStart := lastIndexOf(names, "content_block_start")
	if lastStart > firstStop {
		t.Fatalf("a block was opened after the first block was closed: %v", names)
	}
}

// A stream that ends with both a finish_reason and a [DONE] must emit the
// terminal events once, not twice.
func TestAnthropicStreamConverterStopIsIdempotent(t *testing.T) {
	converter := newAnthropicStreamConverter("hy3")
	var all [][]byte
	all = append(all, converter.consume(frame(t, map[string]any{"content": "hi"}, ""))...)
	all = append(all, converter.consume(frame(t, map[string]any{}, "stop"))...)
	all = append(all, converter.consume([]byte("data: [DONE]\n\n"))...)

	names := eventNames(events(t, all))
	counts := map[string]int{}
	for _, name := range names {
		counts[name]++
	}
	if counts["message_stop"] != 1 || counts["message_delta"] != 1 {
		t.Fatalf("terminal events emitted more than once: %v", names)
	}
}

// Tool calls become tool_use blocks with the arguments streamed as
// input_json_delta, keyed by the upstream tool index.
func TestAnthropicStreamConverterToolCalls(t *testing.T) {
	converter := newAnthropicStreamConverter("hy3")
	var all [][]byte
	all = append(all, converter.consume(frame(t, map[string]any{"tool_calls": []any{
		map[string]any{"index": float64(0), "id": "call_1", "function": map[string]any{"name": "lookup", "arguments": ""}},
	}}, ""))...)
	all = append(all, converter.consume(frame(t, map[string]any{"tool_calls": []any{
		map[string]any{"index": float64(0), "function": map[string]any{"arguments": `{"q":`}},
	}}, ""))...)
	all = append(all, converter.consume(frame(t, map[string]any{"tool_calls": []any{
		map[string]any{"index": float64(0), "function": map[string]any{"arguments": `"x"}`}},
	}}, ""))...)
	all = append(all, converter.consume(frame(t, map[string]any{}, "tool_calls"))...)

	parsed := events(t, all)
	names := eventNames(parsed)
	counts := map[string]int{}
	for _, name := range names {
		counts[name]++
	}
	if counts["content_block_start"] != 1 {
		t.Fatalf("one tool call must open one block, got %d (%v)", counts["content_block_start"], names)
	}
	if counts["content_block_stop"] != 1 {
		t.Fatalf("expected the tool block to be closed once, got %d (%v)", counts["content_block_stop"], names)
	}

	// The three argument fragments must arrive in order and concatenate.
	var joined strings.Builder
	for _, value := range parsed {
		if value.Name != "content_block_delta" {
			continue
		}
		delta, _ := value.Payload["delta"].(map[string]any)
		if stringOr(delta["type"], "") == "input_json_delta" {
			joined.WriteString(stringOr(delta["partial_json"], ""))
		}
	}
	if joined.String() != `{"q":"x"}` {
		t.Fatalf("argument fragments did not reassemble: %q", joined.String())
	}
	// The stop reason must be translated for Anthropic clients.
	last := parsed[len(parsed)-2]
	delta, _ := last.Payload["delta"].(map[string]any)
	if stringOr(delta["stop_reason"], "") != "tool_use" {
		t.Fatalf("finish_reason tool_calls must map to tool_use, got %#v", last.Payload)
	}
}

// A reasoning-only stream still produces a well-formed message: start, one
// thinking block, stop, message_delta, message_stop.
func TestAnthropicStreamConverterReasoningOnly(t *testing.T) {
	converter := newAnthropicStreamConverter("hy4-preview")
	var all [][]byte
	all = append(all, converter.consume(frame(t, map[string]any{"reasoning_content": "pondering"}, ""))...)
	all = append(all, converter.consume([]byte("data: [DONE]\n\n"))...)

	parsed := events(t, all)
	names := eventNames(parsed)
	if names[0] != "message_start" {
		t.Fatalf("first event must be message_start: %v", names)
	}
	if names[len(names)-1] != "message_stop" {
		t.Fatalf("last event must be message_stop: %v", names)
	}
	started := false
	for _, value := range parsed {
		if value.Name != "content_block_start" {
			continue
		}
		block, _ := value.Payload["content_block"].(map[string]any)
		if stringOr(block["type"], "") == "thinking" {
			started = true
		}
	}
	if !started {
		t.Fatalf("reasoning content must open a thinking block: %v", names)
	}
}

func indexOf(values []string, target string) int {
	for index, value := range values {
		if value == target {
			return index
		}
	}
	return -1
}

func lastIndexOf(values []string, target string) int {
	for index := len(values) - 1; index >= 0; index-- {
		if values[index] == target {
			return index
		}
	}
	return -1
}
