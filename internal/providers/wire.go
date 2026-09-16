package providers

import (
	"bufio"
	"encoding/json"
	"io"
	"strings"

	"github.com/dmego/qoderbuddy2api/internal/chatwire"
)

// DoneFrame is the terminal SSE frame every OpenAI-compatible stream ends with.
var DoneFrame = []byte("data: [DONE]\n\n")

// sseReader turns an upstream SSE body into decoded JSON objects, filtering
// non-data lines the same way the Python `_stream_chunk` helper did.
type sseReader struct {
	scanner *bufio.Scanner
	closed  bool
}

func newSSEReader(body io.Reader) *sseReader {
	scanner := bufio.NewScanner(body)
	// Upstream frames can carry long tool-call deltas; the default 64 KiB line
	// cap would abort mid-stream.
	scanner.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
	return &sseReader{scanner: scanner}
}

// Next returns the next decoded chunk. The bool is false once the upstream
// sent `data: [DONE]` or the body ended.
func (r *sseReader) Next() (map[string]any, bool) {
	for r.scanner.Scan() {
		line := r.scanner.Text()
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		data := strings.TrimSpace(line[6:])
		if data == "[DONE]" {
			return nil, false
		}
		var chunk map[string]any
		if err := json.Unmarshal([]byte(data), &chunk); err != nil {
			continue
		}
		normalizeToolCalls(chunk)
		return chunk, true
	}
	return nil, false
}

// Err reports a scanner failure that ended the stream early.
func (r *sseReader) Err() error { return r.scanner.Err() }

// Frame re-encodes a decoded chunk as an SSE frame. Go's encoder escapes the
// same characters Python's json.dumps did, but it also escapes <, > and & by
// default — SetEscapeHTML(false) keeps byte-parity with the upstream payload.
func Frame(chunk map[string]any) ([]byte, error) {
	var builder strings.Builder
	encoder := json.NewEncoder(&builder)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(chunk); err != nil {
		return nil, err
	}
	return []byte("data: " + strings.TrimSuffix(builder.String(), "\n") + "\n\n"), nil
}

// normalizeToolCalls injects a missing index and rewrites Anthropic-style tool
// ids into OpenAI form, matching sse.inject_tool_call_index/normalize_tool_call_id.
func normalizeToolCalls(chunk map[string]any) {
	choices, ok := chunk["choices"].([]any)
	if !ok || len(choices) == 0 {
		return
	}
	choice, ok := choices[0].(map[string]any)
	if !ok {
		return
	}
	delta, ok := choice["delta"].(map[string]any)
	if !ok {
		return
	}
	calls, ok := delta["tool_calls"].([]any)
	if !ok || len(calls) == 0 {
		return
	}
	for index, raw := range calls {
		call, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if _, present := call["index"]; !present {
			call["index"] = index
		}
		if id, ok := call["id"].(string); ok && strings.HasPrefix(id, "tooluse_") {
			call["id"] = "call_" + strings.TrimPrefix(id, "tooluse_")
		}
	}
}

// filterReasoning strips reasoning_content from a frame when the client did not
// ask to see thinking, mirroring worker/streaming._filter_reasoning.
func filterReasoning(frame []byte) []byte {
	text := string(frame)
	line := strings.TrimSpace(text)
	if !strings.HasPrefix(line, "data:") || strings.HasPrefix(line, "data: [DONE]") {
		return frame
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(strings.TrimSpace(line[5:])), &payload); err != nil {
		return frame
	}
	choices, ok := payload["choices"].([]any)
	if !ok {
		return frame
	}
	changed := false
	for _, raw := range choices {
		choice, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		delta, ok := choice["delta"].(map[string]any)
		if !ok {
			continue
		}
		if _, present := delta["reasoning_content"]; present {
			delete(delta, "reasoning_content")
			changed = true
		}
	}
	if !changed {
		return frame
	}
	reframed, err := Frame(payload)
	if err != nil {
		return frame
	}
	return reframed
}

// FrameHasContent reports whether a frame carries user-visible output. The
// first such frame defines the first-token latency.
func FrameHasContent(frame []byte) bool {
	line := strings.TrimSpace(string(frame))
	if !strings.HasPrefix(line, "data: ") || strings.HasPrefix(line, "data: [DONE]") {
		return false
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(strings.TrimSpace(line[6:])), &payload); err != nil {
		return false
	}
	choices, ok := payload["choices"].([]any)
	if !ok {
		return false
	}
	for _, raw := range choices {
		choice, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if reason, ok := choice["finish_reason"].(string); ok && reason != "" {
			continue
		}
		delta, ok := choice["delta"].(map[string]any)
		if !ok {
			continue
		}
		for _, key := range []string{"content", "reasoning_content", "tool_calls"} {
			switch value := delta[key].(type) {
			case string:
				if value != "" {
					return true
				}
			case []any:
				if len(value) > 0 {
					return true
				}
			}
		}
	}
	return false
}

// aggregateStream folds an OpenAI SSE stream into a single completion response,
// matching sse.StreamAggregator.
type aggregator struct {
	model        string
	content      strings.Builder
	reasoning    strings.Builder
	toolCalls    []map[string]any
	finishReason string
	usage        map[string]any
	id           string
	created      int64
}

func newAggregator(model string) *aggregator {
	return &aggregator{model: model, finishReason: "stop"}
}

func (a *aggregator) process(chunk map[string]any) {
	if id, ok := chunk["id"].(string); ok && id != "" {
		a.id = id
	}
	if created, ok := chunk["created"].(float64); ok {
		a.created = int64(created)
	}
	if usage, ok := chunk["usage"].(map[string]any); ok {
		a.usage = usage
	}
	choices, ok := chunk["choices"].([]any)
	if !ok || len(choices) == 0 {
		return
	}
	choice, ok := choices[0].(map[string]any)
	if !ok {
		return
	}
	if delta, ok := choice["delta"].(map[string]any); ok {
		if text, ok := delta["content"].(string); ok {
			a.content.WriteString(text)
		}
		if text, ok := delta["reasoning_content"].(string); ok {
			a.reasoning.WriteString(text)
		}
		if calls, ok := delta["tool_calls"].([]any); ok {
			a.mergeToolCalls(calls)
		}
	}
	if reason, ok := choice["finish_reason"].(string); ok && reason != "" {
		a.finishReason = reason
	}
}

func (a *aggregator) mergeToolCalls(deltas []any) {
	for _, raw := range deltas {
		delta, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		index := 0
		if value, ok := delta["index"].(float64); ok {
			index = int(value)
		}
		for len(a.toolCalls) <= index {
			a.toolCalls = append(a.toolCalls, map[string]any{
				"id":       "",
				"type":     "function",
				"function": map[string]any{"name": "", "arguments": ""},
			})
		}
		target := a.toolCalls[index]
		function, _ := target["function"].(map[string]any)
		if incoming, ok := delta["function"].(map[string]any); ok {
			if name, ok := incoming["name"].(string); ok && name != "" {
				function["name"] = function["name"].(string) + name
			}
			if args, ok := incoming["arguments"].(string); ok && args != "" {
				function["arguments"] = function["arguments"].(string) + args
			}
		}
		if id, ok := delta["id"].(string); ok && id != "" {
			target["id"] = id
		}
	}
}

func (a *aggregator) response() map[string]any {
	message := map[string]any{"role": "assistant"}
	if content := a.content.String(); content != "" {
		message["content"] = content
	} else {
		message["content"] = nil
	}
	if reasoning := a.reasoning.String(); reasoning != "" {
		message["reasoning_content"] = reasoning
	}
	finish := a.finishReason
	if len(a.toolCalls) > 0 {
		for index, call := range a.toolCalls {
			if id, _ := call["id"].(string); id == "" {
				call["id"] = toolCallID(index)
			}
		}
		message["tool_calls"] = a.toolCalls
		finish = "tool_calls"
	}
	usage := a.usage
	if usage == nil {
		usage = map[string]any{"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
	}
	id := a.id
	if id == "" {
		id = "chatcmpl-" + randomHex(12)
	}
	created := a.created
	if created == 0 {
		created = nowUnix()
	}
	return map[string]any{
		"id":      id,
		"object":  "chat.completion",
		"created": created,
		"model":   a.model,
		"choices": []any{map[string]any{
			"index":         0,
			"message":       message,
			"finish_reason": finish,
		}},
		"usage": usage,
	}
}

// requestValues copies the schema fields the upstream accepts, mirroring
// codebuddy._request_values: only non-nil values are forwarded.
func requestValues(request *chatwire.ChatRequest, keys []string) map[string]any {
	out := map[string]any{}
	for _, key := range keys {
		switch key {
		case "temperature":
			if request.Temperature != nil {
				out[key] = *request.Temperature
			}
		case "max_tokens":
			if request.MaxTokens != nil {
				out[key] = *request.MaxTokens
			}
		case "max_completion_tokens":
			if request.MaxCompletionTokens != nil {
				out[key] = *request.MaxCompletionTokens
			}
		case "top_p":
			if request.TopP != nil {
				out[key] = *request.TopP
			}
		case "stop":
			if request.Stop != nil {
				out[key] = request.Stop
			}
		case "presence_penalty":
			if request.PresencePenalty != nil {
				out[key] = *request.PresencePenalty
			}
		case "frequency_penalty":
			if request.FrequencyPenalty != nil {
				out[key] = *request.FrequencyPenalty
			}
		case "n":
			if request.N != nil {
				out[key] = *request.N
			}
		case "response_format":
			if request.ResponseFormat != nil {
				out[key] = request.ResponseFormat
			}
		case "seed":
			if request.Seed != nil {
				out[key] = *request.Seed
			}
		case "user":
			if request.User != "" {
				out[key] = request.User
			}
		default:
			if value, ok := request.ExtraValue(key); ok && value != nil {
				out[key] = value
			}
		}
	}
	return out
}

// toolValues forwards the tools and tool_choice fields when present.
func toolValues(request *chatwire.ChatRequest) map[string]any {
	if len(request.Tools) == 0 {
		return nil
	}
	out := map[string]any{"tools": request.Tools}
	if request.ToolChoice != nil {
		out["tool_choice"] = request.ToolChoice
	}
	return out
}

// claudeSystemSentinels are the Claude/Anthropic identity phrases the upstream
// content filter rejects.
var claudeSystemSentinels = []string{
	"You are Claude Code",
	"You are a Claude agent",
	"Anthropic's official CLI for Claude",
	"Claude Agent SDK",
}

const neutralSystemPrompt = "You are a helpful assistant."

// scrubText replaces a Claude/Anthropic system prompt with a neutral one.
func scrubText(text string) string {
	if text == "" {
		return text
	}
	for _, sentinel := range claudeSystemSentinels {
		if strings.Contains(text, sentinel) {
			return neutralSystemPrompt
		}
	}
	return text
}

// scrubContent scrubs string or multimodal text blocks in a message content.
func scrubContent(content any) any {
	switch typed := content.(type) {
	case string:
		return scrubText(typed)
	case []any:
		out := make([]any, 0, len(typed))
		for _, block := range typed {
			mapping, ok := block.(map[string]any)
			if !ok || mapping["type"] != "text" {
				out = append(out, block)
				continue
			}
			text, ok := mapping["text"].(string)
			if !ok {
				out = append(out, block)
				continue
			}
			copied := map[string]any{}
			for key, value := range mapping {
				copied[key] = value
			}
			copied["text"] = scrubText(text)
			out = append(out, copied)
		}
		return out
	case map[string]any:
		if typed["type"] == "text" {
			if text, ok := typed["text"].(string); ok {
				copied := map[string]any{}
				for key, value := range typed {
					copied[key] = value
				}
				copied["text"] = scrubText(text)
				return copied
			}
		}
		return typed
	}
	return content
}

// outboundMessage folds OpenAI's developer role into system and scrubs system
// content. CodeBuddy answers any request carrying a developer message with 400
// code 11128, while the identical text on the system role passes.
func outboundMessage(message chatwire.Message) map[string]any {
	out := map[string]any{}
	if message.Extra != nil {
		for key, value := range message.Extra {
			out[key] = value
		}
	}
	role := message.Role
	if role == "developer" {
		role = "system"
	}
	out["role"] = role
	if len(message.Content) > 0 {
		var decoded any
		if err := json.Unmarshal(message.Content, &decoded); err == nil {
			if decoded != nil {
				out["content"] = decoded
			}
		}
	}
	if message.Name != "" {
		out["name"] = message.Name
	}
	if len(message.ToolCalls) > 0 {
		out["tool_calls"] = message.ToolCalls
	}
	if message.ToolCallID != "" {
		out["tool_call_id"] = message.ToolCallID
	}
	if out["role"] == "system" {
		if content, ok := out["content"]; ok {
			out["content"] = scrubContent(content)
		}
	}
	return out
}
