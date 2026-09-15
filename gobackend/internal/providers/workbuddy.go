package providers

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/dmego/qoderbuddy2api/gobackend/internal/chatwire"
)

// passthroughKeys are forwarded to upstream verbatim when the client supplied
// them; they are not part of the OpenAI schema but the WorkBuddy gateway
// understands them.
var passthroughKeys = []string{
	"reasoning_effort", "verbosity", "reasoning_summary",
	"thinking", "max_context_tokens", "context_window",
}

// requestKeys are the OpenAI schema fields forwarded upstream.
var requestKeys = []string{
	"temperature", "max_tokens", "max_completion_tokens", "top_p",
	"stop", "presence_penalty", "frequency_penalty", "n",
	"response_format", "seed", "user",
}

// WorkBuddy is the domestic WorkBuddy/CodeBuddy provider (copilot.tencent.com).
//
// The provider identifier stays "codebuddy": accounts, credentials, telemetry
// and the admin console all key off that name, and renaming it would orphan
// every stored row.
type WorkBuddy struct {
	Token         string
	Endpoint      string
	DefaultEffort string
	client        *http.Client
}

// NewWorkBuddy builds a provider for one account credential.
func NewWorkBuddy(token, endpoint, defaultEffort string) *WorkBuddy {
	return &WorkBuddy{
		Token:         token,
		Endpoint:      strings.TrimRight(endpoint, "/"),
		DefaultEffort: strings.ToLower(strings.TrimSpace(defaultEffort)),
		client:        newUpstreamClient(),
	}
}

func (p *WorkBuddy) Name() string { return "codebuddy" }

func (p *WorkBuddy) Complete(ctx context.Context, request *chatwire.ChatRequest) (map[string]any, error) {
	stream, err := p.Stream(ctx, request)
	if err != nil {
		return nil, err
	}
	defer stream.Close()
	agg := newAggregator(request.Model)
	for {
		frame, err := stream.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, err
		}
		if bytes.HasPrefix(frame, []byte("data: [DONE]")) {
			break
		}
		payload := strings.TrimSpace(string(frame))
		if !strings.HasPrefix(payload, "data: ") {
			continue
		}
		var chunk map[string]any
		if err := json.Unmarshal([]byte(strings.TrimSpace(payload[6:])), &chunk); err != nil {
			continue
		}
		agg.process(chunk)
	}
	request.ObserveUsage(agg.usage)
	return agg.response(), nil
}

func (p *WorkBuddy) Stream(ctx context.Context, request *chatwire.ChatRequest) (FrameStream, error) {
	body := p.buildBody(request)
	encoded, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	url := p.Endpoint + "/v2/chat/completions"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(encoded))
	if err != nil {
		return nil, err
	}
	for key, value := range p.headers() {
		req.Header.Set(key, value)
	}
	response, err := p.client.Do(req)
	if err != nil {
		return nil, err
	}
	if response.StatusCode != http.StatusOK {
		defer response.Body.Close()
		raw, _ := io.ReadAll(io.LimitReader(response.Body, 64*1024))
		return nil, ParseUpstreamError(response.StatusCode, string(raw))
	}
	return newUpstreamStream(response), nil
}

func (p *WorkBuddy) Close() error { return nil }

func (p *WorkBuddy) buildBody(request *chatwire.ChatRequest) map[string]any {
	body := map[string]any{
		"model":    request.Model,
		"messages": p.preparedMessages(request),
		"stream":   true,
	}
	mergeValues(body, requestValues(request, requestKeys))
	mergeValues(body, toolValues(request))
	mergeValues(body, requestValues(request, passthroughKeys))
	if p.DefaultEffort != "" {
		if _, present := body["reasoning_effort"]; !present {
			// WorkBuddy models only emit reasoning_content when reasoning_effort
			// is set; inject a default so supported models actually think.
			body["reasoning_effort"] = p.DefaultEffort
			request.RecordEffort(p.DefaultEffort)
		}
	}
	return body
}

// preparedMessages folds the developer role and scrubs system content, and
// guarantees at least two messages because the upstream rejects single-turn
// payloads.
func (p *WorkBuddy) preparedMessages(request *chatwire.ChatRequest) []map[string]any {
	messages := make([]map[string]any, 0, len(request.Messages)+1)
	for _, message := range request.Messages {
		messages = append(messages, outboundMessage(message))
	}
	if len(messages) < 2 {
		messages = append([]map[string]any{{"role": "system", "content": neutralSystemPrompt}}, messages...)
	}
	return messages
}

func (p *WorkBuddy) headers() map[string]string {
	return map[string]string{
		"Content-Type":   "application/json",
		"Authorization":  "Bearer " + p.Token,
		"User-Agent":     "CLI/1.0.8 CodeBuddy/1.0.8",
		"X-Product":      "SaaS",
		"X-Domain":       "copilot.tencent.com",
		"X-Agent-Intent": "CodeCompletion",
		"Accept":         "text/event-stream",
		"X-Machine-Id":   randomUUID(),
		"X-Request-ID":   randomUUID(),
	}
}

// WorkBuddyIntl is the international WorkBuddy provider (www.workbuddy.ai).
//
// Same SSE contract as the domestic pool, plus one extra upstream requirement:
// messages[0] must be a system message, otherwise the gateway answers
// code=11128 "first message is not system prompt".
type WorkBuddyIntl struct {
	Token         string
	Endpoint      string
	DefaultEffort string
	client        *http.Client
}

// IntlFallbackSystemPrompt is prepended when the client's first turn is not a
// system message.
const IntlFallbackSystemPrompt = neutralSystemPrompt

// NewWorkBuddyIntl builds a provider for one international account credential.
func NewWorkBuddyIntl(token, endpoint, defaultEffort string) *WorkBuddyIntl {
	return &WorkBuddyIntl{
		Token:         token,
		Endpoint:      strings.TrimRight(endpoint, "/"),
		DefaultEffort: strings.ToLower(strings.TrimSpace(defaultEffort)),
		client:        newUpstreamClient(),
	}
}

func (p *WorkBuddyIntl) Name() string { return "workbuddy_intl" }

func (p *WorkBuddyIntl) Complete(ctx context.Context, request *chatwire.ChatRequest) (map[string]any, error) {
	stream, err := p.Stream(ctx, request)
	if err != nil {
		return nil, err
	}
	defer stream.Close()
	agg := newAggregator(request.Model)
	for {
		frame, err := stream.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return nil, err
		}
		if bytes.HasPrefix(frame, []byte("data: [DONE]")) {
			break
		}
		payload := strings.TrimSpace(string(frame))
		if !strings.HasPrefix(payload, "data: ") {
			continue
		}
		var chunk map[string]any
		if err := json.Unmarshal([]byte(strings.TrimSpace(payload[6:])), &chunk); err != nil {
			continue
		}
		agg.process(chunk)
	}
	request.ObserveUsage(agg.usage)
	return agg.response(), nil
}

func (p *WorkBuddyIntl) Stream(ctx context.Context, request *chatwire.ChatRequest) (FrameStream, error) {
	body := p.buildBody(request)
	encoded, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	url := p.Endpoint + "/v2/chat/completions"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(encoded))
	if err != nil {
		return nil, err
	}
	for key, value := range p.headers() {
		req.Header.Set(key, value)
	}
	response, err := p.client.Do(req)
	if err != nil {
		return nil, err
	}
	if response.StatusCode != http.StatusOK {
		defer response.Body.Close()
		raw, _ := io.ReadAll(io.LimitReader(response.Body, 64*1024))
		return nil, ParseUpstreamError(response.StatusCode, string(raw))
	}
	return newUpstreamStream(response), nil
}

func (p *WorkBuddyIntl) Close() error { return nil }

func (p *WorkBuddyIntl) buildBody(request *chatwire.ChatRequest) map[string]any {
	body := map[string]any{
		"model":    request.Model,
		"messages": intlMessages(request),
		"stream":   true,
	}
	mergeValues(body, requestValues(request, requestKeys))
	mergeValues(body, toolValues(request))
	mergeValues(body, requestValues(request, passthroughKeys))
	if p.DefaultEffort != "" {
		if _, present := body["reasoning_effort"]; !present {
			body["reasoning_effort"] = p.DefaultEffort
			request.RecordEffort(p.DefaultEffort)
		}
	}
	return body
}

func (p *WorkBuddyIntl) headers() map[string]string {
	return map[string]string{
		"Content-Type":  "application/json",
		"Authorization": "Bearer " + p.Token,
		"Accept":        "text/event-stream",
		"User-Agent":    "CLI/1.0.8 CodeBuddy/1.0.8",
		"X-Product":     "SaaS",
		"X-Domain":      "www.workbuddy.ai",
		"X-Machine-Id":  randomUUID(),
		"X-Request-ID":  randomUUID(),
	}
}

// intlMessages builds outbound messages with a guaranteed leading system turn.
func intlMessages(request *chatwire.ChatRequest) []map[string]any {
	messages := make([]map[string]any, 0, len(request.Messages)+1)
	for _, message := range request.Messages {
		messages = append(messages, outboundMessage(message))
	}
	if len(messages) == 0 || messages[0]["role"] != "system" {
		messages = append([]map[string]any{{"role": "system", "content": IntlFallbackSystemPrompt}}, messages...)
	}
	return messages
}

// upstreamStream adapts an HTTP SSE body into downstream frames.
type upstreamStream struct {
	response *http.Response
	reader   *sseReader
	done     bool
}

func newUpstreamStream(response *http.Response) *upstreamStream {
	return &upstreamStream{response: response, reader: newSSEReader(response.Body)}
}

func (s *upstreamStream) Next() ([]byte, error) {
	if s.done {
		return nil, io.EOF
	}
	chunk, ok := s.reader.Next()
	if !ok {
		s.done = true
		if err := s.reader.Err(); err != nil {
			return nil, err
		}
		return DoneFrame, nil
	}
	return Frame(chunk)
}

func (s *upstreamStream) Close() error {
	s.done = true
	return s.response.Body.Close()
}

// newUpstreamClient builds the HTTP client for upstream calls.
//
// Proxy environment variables are deliberately ignored: this process runs next
// to a TUN-mode proxy on the developer's machine, and a half-dead pooled
// connection through it cost 200+ second stalls on non-streaming requests.
func newUpstreamClient() *http.Client {
	transport := &http.Transport{
		Proxy:                 nil,
		MaxIdleConns:          100,
		MaxIdleConnsPerHost:   16,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
		ForceAttemptHTTP2:     true,
	}
	return &http.Client{
		Transport: transport,
		// No overall timeout: streaming responses legitimately run for minutes.
		// Connection establishment is bounded by the transport instead.
		Timeout: 0,
	}
}

func mergeValues(target map[string]any, values map[string]any) {
	for key, value := range values {
		target[key] = value
	}
}

func randomUUID() string {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "00000000-0000-4000-8000-000000000000"
	}
	raw[6] = (raw[6] & 0x0f) | 0x40
	raw[8] = (raw[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", raw[0:4], raw[4:6], raw[6:8], raw[8:10], raw[10:16])
}

func randomHex(n int) string {
	raw := make([]byte, (n+1)/2)
	if _, err := rand.Read(raw); err != nil {
		return strings.Repeat("0", n)
	}
	return hex.EncodeToString(raw)[:n]
}

func nowUnix() int64 { return time.Now().Unix() }

func toolCallID(index int) string { return "call_" + randomHex(16) + fmt.Sprint(index) }
