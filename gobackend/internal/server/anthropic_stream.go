package server

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"
)

// anthropicStreamConverter translates an OpenAI SSE stream into Anthropic
// Messages SSE events.
//
// Mirrors qb2api/anthropic_stream.py exactly, including two behaviours that are
// easy to get wrong: content blocks are opened once and only closed at the very
// end (never mid-stream), and reasoning_content becomes a thinking block so
// thinking models stay visible to Anthropic clients.
type anthropicStreamConverter struct {
	model string

	started     bool
	stopped     bool
	messageID   string
	nextIndex   int
	activeBlock []int
	textIndex   *int
	thinkIndex  *int
	toolIndexes map[int]int
}

func newAnthropicStreamConverter(model string) *anthropicStreamConverter {
	return &anthropicStreamConverter{model: model, messageID: "msg_" + randomToken(16), toolIndexes: map[int]int{}}
}

// consume turns one OpenAI frame into zero or more Anthropic SSE events.
func (c *anthropicStreamConverter) consume(frame []byte) [][]byte {
	events := c.start()
	for _, line := range strings.Split(string(bytes.TrimSpace(frame)), "\n") {
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		data := strings.TrimSpace(line[6:])
		if data == "[DONE]" {
			return append(events, c.stop("end_turn")...)
		}
		var chunk map[string]any
		if err := json.Unmarshal([]byte(data), &chunk); err != nil {
			continue
		}
		events = append(events, c.processChunk(chunk)...)
	}
	return events
}

// start emits message_start exactly once, before any content event.
func (c *anthropicStreamConverter) start() [][]byte {
	if c.started {
		return nil
	}
	c.started = true
	return [][]byte{anthropicSSE("message_start", map[string]any{
		"type": "message_start",
		"message": map[string]any{
			"id":            c.messageID,
			"type":          "message",
			"role":          "assistant",
			"model":         c.model,
			"content":       []any{},
			"stop_reason":   nil,
			"stop_sequence": nil,
			"usage":         map[string]any{"input_tokens": 0, "output_tokens": 0},
		},
	})}
}

func (c *anthropicStreamConverter) processChunk(chunk map[string]any) [][]byte {
	choices, _ := chunk["choices"].([]any)
	if len(choices) == 0 {
		return nil
	}
	choice, _ := choices[0].(map[string]any)
	delta, _ := choice["delta"].(map[string]any)

	events := c.processDelta(delta)
	if reason := stringOr(choice["finish_reason"], ""); reason != "" {
		mapped, _ := mapFinishReason(reason).(string)
		events = append(events, c.stop(mapped)...)
	}
	return events
}

func (c *anthropicStreamConverter) processDelta(delta map[string]any) [][]byte {
	if delta == nil {
		return nil
	}
	var events [][]byte
	if text := stringOr(delta["reasoning_content"], ""); text != "" {
		events = append(events, c.thinkingDelta(text)...)
	}
	if text := stringOr(delta["content"], ""); text != "" {
		events = append(events, c.textDelta(text)...)
	}
	if calls, ok := delta["tool_calls"].([]any); ok {
		for _, raw := range calls {
			call, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			events = append(events, c.toolDelta(call)...)
		}
	}
	return events
}

// stop closes every active block, then emits message_delta and message_stop.
// It is idempotent: a stream that ends with both a finish_reason and a [DONE]
// must not emit the terminal events twice.
func (c *anthropicStreamConverter) stop(stopReason string) [][]byte {
	if c.stopped {
		return nil
	}
	events := c.start()
	for _, index := range c.activeBlock {
		events = append(events, anthropicSSE("content_block_stop", map[string]any{
			"type": "content_block_stop", "index": index,
		}))
	}
	c.activeBlock = nil
	if stopReason == "" {
		stopReason = "end_turn"
	}
	events = append(events,
		anthropicSSE("message_delta", map[string]any{
			"type":  "message_delta",
			"delta": map[string]any{"stop_reason": stopReason, "stop_sequence": nil},
			"usage": map[string]any{"output_tokens": 0},
		}),
		anthropicSSE("message_stop", map[string]any{"type": "message_stop"}),
	)
	c.stopped = true
	return events
}

func (c *anthropicStreamConverter) textDelta(text string) [][]byte {
	var events [][]byte
	if c.textIndex == nil {
		index := c.reserveBlock()
		c.textIndex = &index
		events = append(events, anthropicSSE("content_block_start", map[string]any{
			"type":          "content_block_start",
			"index":         index,
			"content_block": map[string]any{"type": "text", "text": ""},
		}))
	}
	events = append(events, anthropicSSE("content_block_delta", map[string]any{
		"type":  "content_block_delta",
		"index": *c.textIndex,
		"delta": map[string]any{"type": "text_delta", "text": text},
	}))
	return events
}

// thinkingDelta forwards upstream reasoning_content as an Anthropic thinking
// block, which is how a reasoning model's output reaches Anthropic clients.
func (c *anthropicStreamConverter) thinkingDelta(text string) [][]byte {
	var events [][]byte
	if c.thinkIndex == nil {
		index := c.reserveBlock()
		c.thinkIndex = &index
		events = append(events, anthropicSSE("content_block_start", map[string]any{
			"type":          "content_block_start",
			"index":         index,
			"content_block": map[string]any{"type": "thinking", "thinking": "", "signature": ""},
		}))
	}
	events = append(events, anthropicSSE("content_block_delta", map[string]any{
		"type":  "content_block_delta",
		"index": *c.thinkIndex,
		"delta": map[string]any{"type": "thinking_delta", "thinking": text},
	}))
	return events
}

func (c *anthropicStreamConverter) toolDelta(call map[string]any) [][]byte {
	openAIIndex := 0
	if value, ok := call["index"].(float64); ok {
		openAIIndex = int(value)
	}
	function, _ := call["function"].(map[string]any)
	var events [][]byte
	index, opened := c.toolIndexes[openAIIndex]
	if !opened {
		index = c.reserveBlock()
		c.toolIndexes[openAIIndex] = index
		id := stringOr(call["id"], "")
		if id == "" {
			id = "toolu_" + randomToken(16)
		}
		name := stringOr(function["name"], "")
		if name == "" {
			name = fmt.Sprintf("tool_%d", openAIIndex)
		}
		events = append(events, anthropicSSE("content_block_start", map[string]any{
			"type":  "content_block_start",
			"index": index,
			"content_block": map[string]any{
				"type": "tool_use", "id": id, "name": name, "input": map[string]any{},
			},
		}))
	}
	if arguments := stringOr(function["arguments"], ""); arguments != "" {
		events = append(events, anthropicSSE("content_block_delta", map[string]any{
			"type":  "content_block_delta",
			"index": index,
			"delta": map[string]any{"type": "input_json_delta", "partial_json": arguments},
		}))
	}
	return events
}

// reserveBlock allocates the next content block index and marks it active.
func (c *anthropicStreamConverter) reserveBlock() int {
	index := c.nextIndex
	c.nextIndex++
	c.activeBlock = append(c.activeBlock, index)
	return index
}

// anthropicSSE frames one Anthropic event.
func anthropicSSE(event string, payload map[string]any) []byte {
	encoded, err := json.Marshal(payload)
	if err != nil {
		encoded = []byte("{}")
	}
	return []byte(fmt.Sprintf("event: %s\ndata: %s\n\n", event, string(encoded)))
}

// anthropicErrorSSE frames an in-stream error event.
func anthropicErrorSSE(message string) []byte {
	return anthropicSSE("error", map[string]any{
		"type":  "error",
		"error": map[string]any{"type": "api_error", "message": message},
	})
}
