// Package chatwire holds the OpenAI-compatible wire types shared by the
// inbound HTTP surface and the upstream providers.
package chatwire

import (
	"encoding/json"
	"strings"
)

// Message is one chat turn. Unknown fields are kept in Extra so provider
// pass-through stays lossless, mirroring pydantic's extra="allow".
type Message struct {
	Role       string          `json:"role"`
	Content    json.RawMessage `json:"content,omitempty"`
	Name       string          `json:"name,omitempty"`
	ToolCalls  []ToolCall      `json:"tool_calls,omitempty"`
	ToolCallID string          `json:"tool_call_id,omitempty"`
	Extra      map[string]any  `json:"-"`
}

// UnmarshalJSON captures both the known fields and any extra keys.
func (m *Message) UnmarshalJSON(data []byte) error {
	type alias Message
	var known alias
	if err := json.Unmarshal(data, &known); err != nil {
		return err
	}
	*m = Message(known)
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil
	}
	for _, field := range []string{"role", "content", "name", "tool_calls", "tool_call_id"} {
		delete(raw, field)
	}
	if len(raw) > 0 {
		m.Extra = make(map[string]any, len(raw))
		for key, value := range raw {
			var decoded any
			if err := json.Unmarshal(value, &decoded); err == nil {
				m.Extra[key] = decoded
			}
		}
	}
	return nil
}

// MarshalJSON re-emits Extra alongside the known fields.
func (m Message) MarshalJSON() ([]byte, error) {
	out := map[string]any{}
	if m.Extra != nil {
		for key, value := range m.Extra {
			out[key] = value
		}
	}
	out["role"] = m.Role
	if len(m.Content) > 0 {
		var decoded any
		if err := json.Unmarshal(m.Content, &decoded); err == nil {
			if decoded != nil {
				out["content"] = decoded
			}
		} else {
			out["content"] = string(m.Content)
		}
	}
	if m.Name != "" {
		out["name"] = m.Name
	}
	if len(m.ToolCalls) > 0 {
		out["tool_calls"] = m.ToolCalls
	}
	if m.ToolCallID != "" {
		out["tool_call_id"] = m.ToolCallID
	}
	return json.Marshal(out)
}

// ToolCall is one OpenAI tool invocation.
type ToolCall struct {
	Index    *int         `json:"index,omitempty"`
	ID       string       `json:"id,omitempty"`
	Type     string       `json:"type,omitempty"`
	Function ToolFunction `json:"function"`
}

// ToolFunction is the function payload of a tool call.
type ToolFunction struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

// Tool is one entry of the request "tools" list.
type Tool struct {
	Type     string         `json:"type"`
	Function map[string]any `json:"function"`
}

// ChatRequest mirrors openai.ChatCompletionRequest with extra="allow".
type ChatRequest struct {
	Model               string         `json:"model"`
	Messages            []Message      `json:"messages"`
	Stream              bool           `json:"stream"`
	Tools               []Tool         `json:"tools,omitempty"`
	ToolChoice          any            `json:"tool_choice,omitempty"`
	Temperature         *float64       `json:"temperature,omitempty"`
	MaxTokens           *int           `json:"max_tokens,omitempty"`
	MaxCompletionTokens *int           `json:"max_completion_tokens,omitempty"`
	TopP                *float64       `json:"top_p,omitempty"`
	Stop                any            `json:"stop,omitempty"`
	PresencePenalty     *float64       `json:"presence_penalty,omitempty"`
	FrequencyPenalty    *float64       `json:"frequency_penalty,omitempty"`
	N                   *int           `json:"n,omitempty"`
	ResponseFormat      map[string]any `json:"response_format,omitempty"`
	Seed                *int           `json:"seed,omitempty"`
	User                string         `json:"user,omitempty"`
	ReasoningEffort     string         `json:"reasoning_effort,omitempty"`
	Extra               map[string]any `json:"-"`

	// Request-scoped telemetry, filled in by the routing layer.
	SelectedProvider string
	SelectedAccount  string
	StreamCommitted  bool
	InputTokens      *int
	OutputTokens     *int
	EffectiveEffort  string
	StreamError      string

	// FirstTokenAt is set by the stream adapters when the first content-bearing
	// chunk reaches the client; it drives the first-token latency metric.
	FirstTokenRecorded bool
}

// UnmarshalJSON captures extra fields the same way pydantic extra="allow" did.
func (r *ChatRequest) UnmarshalJSON(data []byte) error {
	type alias ChatRequest
	var known alias
	if err := json.Unmarshal(data, &known); err != nil {
		return err
	}
	*r = ChatRequest(known)
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil
	}
	for _, field := range []string{
		"model", "messages", "stream", "tools", "tool_choice", "temperature",
		"max_tokens", "max_completion_tokens", "top_p", "stop", "presence_penalty",
		"frequency_penalty", "n", "response_format", "seed", "user", "reasoning_effort",
	} {
		delete(raw, field)
	}
	if len(raw) > 0 {
		r.Extra = make(map[string]any, len(raw))
		for key, value := range raw {
			var decoded any
			if err := json.Unmarshal(value, &decoded); err == nil {
				r.Extra[key] = decoded
			}
		}
	}
	return nil
}

// ExtraValue returns an extra (non-schema) request field as a string, used for
// pass-through keys such as verbosity and thinking.
func (r *ChatRequest) ExtraValue(key string) (any, bool) {
	if r.Extra == nil {
		return nil, false
	}
	value, ok := r.Extra[key]
	return value, ok
}

// RecordProvider notes which provider pool served this request.
func (r *ChatRequest) RecordProvider(provider string) { r.SelectedProvider = provider }

// RecordEffort notes the reasoning effort actually sent upstream.
func (r *ChatRequest) RecordEffort(effort string) { r.EffectiveEffort = effort }

// RecordStreamError notes the exception a stream adapter converted into SSE.
func (r *ChatRequest) RecordStreamError(code string) { r.StreamError = code }

// RecordSlot notes the account slot and whether the stream had committed.
func (r *ChatRequest) RecordSlot(slotKey string, committed bool) {
	if index := strings.LastIndex(slotKey, ":"); index >= 0 {
		r.SelectedAccount = slotKey[index+1:]
	} else {
		r.SelectedAccount = slotKey
	}
	r.StreamCommitted = r.StreamCommitted || committed
}

// ObserveUsage records token counts from an upstream usage object.
func (r *ChatRequest) ObserveUsage(usage map[string]any) {
	if usage == nil {
		return
	}
	if value, ok := tokenValue(usage, "prompt_tokens", "input_tokens"); ok {
		r.InputTokens = &value
	}
	if value, ok := tokenValue(usage, "completion_tokens", "output_tokens"); ok {
		r.OutputTokens = &value
	}
}

// ObserveStreamChunk records usage found in an already-framed SSE frame.
func (r *ChatRequest) ObserveStreamChunk(chunk []byte) {
	text := string(chunk)
	for _, line := range strings.Split(text, "\n") {
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		payload := strings.TrimSpace(line[6:])
		if payload == "[DONE]" || payload == "" {
			continue
		}
		var body map[string]any
		if err := json.Unmarshal([]byte(payload), &body); err != nil {
			continue
		}
		if usage, ok := body["usage"].(map[string]any); ok {
			r.ObserveUsage(usage)
		}
	}
}

// Telemetry renders the request-scoped fields the event writer consumes.
func (r *ChatRequest) Telemetry() map[string]any {
	effort := r.EffectiveEffort
	if effort == "" {
		effort = r.ReasoningEffort
	}
	out := map[string]any{
		"provider":         nullableString(r.SelectedProvider),
		"account_id":       nullableString(r.SelectedAccount),
		"stream_committed": r.StreamCommitted,
		"input_tokens":     intOrNil(r.InputTokens),
		"output_tokens":    intOrNil(r.OutputTokens),
		"reasoning_effort": nullableString(effort),
	}
	return out
}

func tokenValue(usage map[string]any, names ...string) (int, bool) {
	for _, name := range names {
		switch value := usage[name].(type) {
		case int:
			if value >= 0 {
				return value, true
			}
		case float64:
			if value >= 0 {
				return int(value), true
			}
		case json.Number:
			if parsed, err := value.Int64(); err == nil && parsed >= 0 {
				return int(parsed), true
			}
		}
	}
	return 0, false
}

func nullableString(value string) any {
	if value == "" {
		return nil
	}
	return value
}

func intOrNil(value *int) any {
	if value == nil {
		return nil
	}
	return *value
}
