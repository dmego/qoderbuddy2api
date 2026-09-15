package server

import (
	"encoding/json"
	"fmt"
	"strings"
)

// anthropicToOpenAI converts an Anthropic Messages request into an OpenAI chat
// request, preserving block ordering. Mirrors qb2api/anthropic.py.
func anthropicToOpenAI(body map[string]any) (map[string]any, error) {
	if body == nil {
		return nil, fmt.Errorf("request body must be an object")
	}
	messages := []any{}
	if system := contentToText(body["system"]); system != "" {
		messages = append(messages, map[string]any{"role": "system", "content": system})
	}
	rawMessages, _ := body["messages"].([]any)
	for _, raw := range rawMessages {
		message, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		messages = append(messages, anthropicMessageToOpenAI(message)...)
	}
	request := map[string]any{
		"model":    body["model"],
		"messages": messages,
		"stream":   boolOr(body["stream"], false),
	}
	for key, value := range completionOptions(body) {
		request[key] = value
	}
	for key, value := range metadataOptions(body) {
		request[key] = value
	}
	for key, value := range toolOptions(body) {
		request[key] = value
	}
	for _, key := range []string{"reasoning_effort", "context_window", "max_context_tokens"} {
		if value, ok := body[key]; ok && value != nil {
			request[key] = value
		}
	}
	return request, nil
}

func completionOptions(body map[string]any) map[string]any {
	out := map[string]any{}
	if value, ok := body["max_tokens"]; ok && value != nil {
		out["max_tokens"] = value
	}
	for _, pair := range [][2]string{{"temperature", "temperature"}, {"top_p", "top_p"}, {"stop_sequences", "stop"}} {
		if value, ok := body[pair[0]]; ok && value != nil {
			out[pair[1]] = value
		}
	}
	return out
}

func metadataOptions(body map[string]any) map[string]any {
	metadata, ok := body["metadata"].(map[string]any)
	if !ok {
		return nil
	}
	if user, ok := metadata["user_id"].(string); ok && user != "" {
		return map[string]any{"user": user}
	}
	return nil
}

func toolOptions(body map[string]any) map[string]any {
	out := map[string]any{}
	rawTools, _ := body["tools"].([]any)
	tools := make([]any, 0, len(rawTools))
	for _, raw := range rawTools {
		tool, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		tools = append(tools, map[string]any{
			"type": "function",
			"function": map[string]any{
				"name":        stringOr(tool["name"], ""),
				"description": stringOr(tool["description"], ""),
				"parameters":  mapOr(tool["input_schema"], map[string]any{"type": "object", "properties": map[string]any{}}),
			},
		})
	}
	if len(tools) > 0 {
		out["tools"] = tools
	}
	if choice := anthropicToolChoice(body["tool_choice"]); choice != nil {
		out["tool_choice"] = choice
	}
	return out
}

func anthropicToolChoice(raw any) any {
	switch typed := raw.(type) {
	case nil:
		return nil
	case string:
		return typed
	case map[string]any:
		switch stringOr(typed["type"], "") {
		case "auto":
			return "auto"
		case "any":
			return "required"
		case "none":
			return "none"
		case "tool":
			return map[string]any{"type": "function", "function": map[string]any{"name": stringOr(typed["name"], "")}}
		}
		return typed
	}
	return raw
}

// anthropicMessageToOpenAI converts one Anthropic message into one or more
// OpenAI messages, splitting tool_result blocks into tool turns.
func anthropicMessageToOpenAI(message map[string]any) []any {
	role := stringOr(message["role"], "user")
	content := message["content"]
	if role == "assistant" {
		if blocks, ok := content.([]any); ok {
			return []any{assistantMessage(blocks)}
		}
	}
	if text, ok := content.(string); ok {
		return []any{map[string]any{"role": role, "content": text}}
	}
	blocks, ok := content.([]any)
	if !ok {
		return []any{map[string]any{"role": role, "content": contentToText(content)}}
	}
	out := []any{}
	textParts := []string{}
	for _, raw := range blocks {
		block, ok := raw.(map[string]any)
		if !ok {
			textParts = append(textParts, contentToText(raw))
			continue
		}
		switch stringOr(block["type"], "") {
		case "text":
			textParts = append(textParts, stringOr(block["text"], ""))
		case "tool_result":
			if len(textParts) > 0 {
				out = append(out, map[string]any{"role": role, "content": strings.Join(nonEmpty(textParts), "\n")})
				textParts = textParts[:0]
			}
			toolCallID := stringOr(block["tool_use_id"], stringOr(block["id"], ""))
			out = append(out, map[string]any{
				"role":         "tool",
				"tool_call_id": toolCallID,
				"content":      contentToText(block["content"]),
			})
		case "image":
			textParts = append(textParts, "[image]")
		default:
			textParts = append(textParts, contentToText(block))
		}
	}
	if len(textParts) > 0 {
		out = append(out, map[string]any{"role": role, "content": strings.Join(nonEmpty(textParts), "\n")})
	}
	if len(out) == 0 {
		return []any{map[string]any{"role": role, "content": ""}}
	}
	return out
}

func assistantMessage(blocks []any) map[string]any {
	textParts := []string{}
	toolCalls := []any{}
	for _, raw := range blocks {
		block, ok := raw.(map[string]any)
		if !ok {
			textParts = append(textParts, contentToText(raw))
			continue
		}
		switch stringOr(block["type"], "") {
		case "text":
			textParts = append(textParts, stringOr(block["text"], ""))
		case "tool_use":
			arguments, _ := json.Marshal(mapOr(block["input"], map[string]any{}))
			id := stringOr(block["id"], "")
			if id == "" {
				id = "toolu_" + randomToken(12)
			}
			toolCalls = append(toolCalls, map[string]any{
				"id":   id,
				"type": "function",
				"function": map[string]any{
					"name":      stringOr(block["name"], ""),
					"arguments": string(arguments),
				},
			})
		}
	}
	message := map[string]any{
		"role":    "assistant",
		"content": strings.Join(nonEmpty(textParts), "\n"),
	}
	if len(toolCalls) > 0 {
		message["tool_calls"] = toolCalls
	}
	return message
}

// openAIToAnthropic converts a non-streaming OpenAI completion into an
// Anthropic message response.
func openAIToAnthropic(response map[string]any, model string) map[string]any {
	choices, _ := response["choices"].([]any)
	choice := map[string]any{}
	if len(choices) > 0 {
		choice, _ = choices[0].(map[string]any)
	}
	message, _ := choice["message"].(map[string]any)
	if message == nil {
		message = map[string]any{}
	}
	content := []any{}
	if thinking := stringOr(message["reasoning_content"], ""); thinking != "" {
		content = append(content, map[string]any{"type": "thinking", "thinking": thinking, "signature": ""})
	}
	if text := stringOr(message["content"], ""); text != "" {
		content = append(content, map[string]any{"type": "text", "text": text})
	}
	if calls, ok := message["tool_calls"].([]any); ok {
		for _, raw := range calls {
			call, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			function, _ := call["function"].(map[string]any)
			id := stringOr(call["id"], "")
			if id == "" {
				id = "toolu_" + randomToken(16)
			}
			content = append(content, map[string]any{
				"type":  "tool_use",
				"id":    id,
				"name":  stringOr(function["name"], ""),
				"input": parseToolInput(function["arguments"]),
			})
		}
	}
	usage, _ := response["usage"].(map[string]any)
	return map[string]any{
		"id":            anthropicMessageID(response["id"]),
		"type":          "message",
		"role":          "assistant",
		"model":         model,
		"content":       content,
		"stop_reason":   mapFinishReason(choice["finish_reason"]),
		"stop_sequence": nil,
		"usage": map[string]any{
			"input_tokens":  intFrom(usage, "prompt_tokens", "input_tokens"),
			"output_tokens": intFrom(usage, "completion_tokens", "output_tokens"),
		},
	}
}

func anthropicMessageID(raw any) string {
	id := stringOr(raw, "")
	if strings.HasPrefix(id, "msg_") {
		return id
	}
	suffix := strings.TrimPrefix(id, "chatcmpl-")
	if suffix == "" {
		suffix = randomToken(16)
	}
	return "msg_" + suffix
}

func mapFinishReason(reason any) any {
	text, ok := reason.(string)
	if !ok || text == "" {
		return nil
	}
	switch text {
	case "stop":
		return "end_turn"
	case "length":
		return "max_tokens"
	case "tool_calls":
		return "tool_use"
	case "content_filter":
		return "stop_sequence"
	}
	return text
}

func parseToolInput(raw any) any {
	switch typed := raw.(type) {
	case map[string]any:
		return typed
	case string:
		if typed == "" {
			return map[string]any{}
		}
		var parsed any
		if err := json.Unmarshal([]byte(typed), &parsed); err != nil {
			return map[string]any{"_raw": typed}
		}
		if mapping, ok := parsed.(map[string]any); ok {
			return mapping
		}
		return map[string]any{"value": parsed}
	}
	return map[string]any{}
}

// contentToText flattens an Anthropic content value into plain text.
func contentToText(content any) string {
	switch typed := content.(type) {
	case nil:
		return ""
	case string:
		return typed
	case []any:
		parts := []string{}
		for _, item := range typed {
			mapping, ok := item.(map[string]any)
			if !ok {
				parts = append(parts, contentToText(item))
				continue
			}
			switch stringOr(mapping["type"], "") {
			case "text":
				parts = append(parts, stringOr(mapping["text"], ""))
			case "tool_result":
				parts = append(parts, contentToText(mapping["content"]))
			case "image":
				parts = append(parts, "[image]")
			default:
				encoded, _ := json.Marshal(mapping)
				parts = append(parts, string(encoded))
			}
		}
		return strings.Join(nonEmpty(parts), "\n")
	case map[string]any:
		if stringOr(typed["type"], "") == "text" {
			return stringOr(typed["text"], "")
		}
		encoded, _ := json.Marshal(typed)
		return string(encoded)
	}
	return fmt.Sprint(content)
}

func nonEmpty(values []string) []string {
	out := make([]string, 0, len(values))
	for _, value := range values {
		if value != "" {
			out = append(out, value)
		}
	}
	return out
}

func boolOr(value any, fallback bool) bool {
	if flag, ok := value.(bool); ok {
		return flag
	}
	return fallback
}

func stringOr(value any, fallback string) string {
	if text, ok := value.(string); ok {
		return text
	}
	return fallback
}

func mapOr(value any, fallback map[string]any) map[string]any {
	if mapping, ok := value.(map[string]any); ok {
		return mapping
	}
	return fallback
}

func intFrom(usage map[string]any, names ...string) int {
	for _, name := range names {
		switch value := usage[name].(type) {
		case float64:
			return int(value)
		case int:
			return value
		}
	}
	return 0
}
