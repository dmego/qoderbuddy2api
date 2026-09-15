package growth

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"
)

// The active day is lit by holding one real chat turn against the CodeBuddy
// console over ACP (JSON-RPC framed as SSE). The model is fixed upstream.
const (
	acpModel         = "hy3"
	acpDefaultPrompt = "你好"
)

// ActiveDayError carries a safe, loggable failure code. The code is what lands
// in workbuddy_active_days.error_code, so it must never embed credentials or
// upstream bodies.
type ActiveDayError struct {
	Code string
}

func (e *ActiveDayError) Error() string { return e.Code }

func activeDayError(code string) error { return &ActiveDayError{Code: code} }

// ErrorCode extracts the safe code from an error, falling back to the Go type
// name exactly like the Python `type(error).__name__` fallback.
func ErrorCode(err error) string {
	var target *ActiveDayError
	if errors.As(err, &target) {
		return target.Code
	}
	return typeName(err)
}

// ActiveDayClient drives one ACP conversation per call.
type ActiveDayClient struct {
	baseURL string
	origin  string
	timeout time.Duration
	// jsonClient bounds the conversation/session bootstrap calls.
	jsonClient *http.Client
	// streamClient has no overall deadline because the SSE channel and the RPC
	// responses are long-lived; ResponseHeaderTimeout still bounds a stalled
	// handshake.
	streamClient *http.Client
	// turnOverride replaces the protocol with a stub. It exists so the
	// one-per-day lock can be exercised without spending a real upstream turn.
	turnOverride func(ctx context.Context, accessToken, prompt string) error
}

// SetTurnForTest substitutes the ACP conversation with a stub. It never
// validates an upstream token, so callers must still supply one.
func (c *ActiveDayClient) SetTurnForTest(turn func(ctx context.Context, accessToken, prompt string) error) {
	c.turnOverride = turn
}

// NewActiveDayClient builds the ACP client. timeout bounds each RPC and the
// final end-turn wait.
func NewActiveDayClient(baseURL string, timeout time.Duration) *ActiveDayClient {
	if timeout <= 0 {
		timeout = 60 * time.Second
	}
	base := strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if base == "" {
		base = defaultCodeBuddyEndpoint
	}
	transport := func() *http.Transport {
		return &http.Transport{
			// Proxy disabled for the same reason as the growth client: a pooled
			// connection through this machine's TUN proxy stalled.
			Proxy:                 nil,
			MaxIdleConnsPerHost:   4,
			ResponseHeaderTimeout: timeout,
		}
	}
	return &ActiveDayClient{
		baseURL:      base,
		origin:       originOf(base),
		timeout:      timeout,
		jsonClient:   &http.Client{Transport: transport(), Timeout: timeout},
		streamClient: &http.Client{Transport: transport(), Timeout: 0},
	}
}

// Run performs the full ACP conversation. A nil error means the upstream sent
// the end-turn notification, which is the only completion signal.
func (c *ActiveDayClient) Run(ctx context.Context, accessToken, prompt string) error {
	if strings.TrimSpace(accessToken) == "" {
		return activeDayError("access_token_missing")
	}
	if prompt == "" {
		prompt = acpDefaultPrompt
	}
	if c.turnOverride != nil {
		return c.turnOverride(ctx, accessToken, prompt)
	}
	session := &acpSession{
		client:   c,
		pending:  map[int]chan rpcResult{},
		turnDone: make(chan struct{}),
	}
	defer session.disconnect()
	if err := session.protocol(ctx, accessToken, prompt); err != nil {
		var active *ActiveDayError
		if errors.As(err, &active) {
			return err
		}
		if errors.Is(err, context.DeadlineExceeded) {
			return activeDayError("rpc_timeout")
		}
		if errors.Is(err, context.Canceled) {
			return activeDayError("rpc_failed:context.Canceled")
		}
		return activeDayError("rpc_failed:" + typeName(err))
	}
	return nil
}

// conversationHeaders are the console REST headers. The browser fingerprint is
// required by the same gateway that guards the growth endpoints.
func (c *ActiveDayClient) conversationHeaders(accessToken string) map[string]string {
	return map[string]string{
		"Authorization": "Bearer " + accessToken,
		"Accept":        "application/json",
		"User-Agent":    acpUserAgent,
		"Origin":        c.origin,
		"Referer":       c.origin + "/",
	}
}

const acpUserAgent = "Mozilla/5.0"

// acpSession holds the per-conversation state: the SSE link, the negotiated
// connection ids, the pending JSON-RPC futures and the end-turn signal.
type acpSession struct {
	client *ActiveDayClient

	link            string
	connectionID    string
	acpSessionToken string
	sessionToken    string

	mu      sync.Mutex
	nextID  int
	pending map[int]chan rpcResult

	turnDone chan struct{}
	turnOnce sync.Once

	streamErr   error
	cancelSSE   context.CancelFunc
	sseBody     io.Closer
	streamGroup sync.WaitGroup
}

type rpcResult struct {
	result map[string]any
	err    error
}

// protocol walks the message sequence: conversation -> session info -> SSE ->
// initialize -> session/new -> set model -> prompt -> end turn.
func (s *acpSession) protocol(ctx context.Context, accessToken, prompt string) error {
	conversationID, err := s.createConversation(ctx, accessToken, prompt)
	if err != nil {
		return err
	}
	session, err := s.sessionInfo(ctx, accessToken, conversationID)
	if err != nil {
		return err
	}
	link, _ := session["link"].(string)
	if link == "" {
		return activeDayError("session_info_missing")
	}
	s.link = link
	// The session response may mint a narrower token for every subsequent ACP
	// call; an absent or blank token means "reuse the access token".
	if token, ok := session["token"].(string); ok && strings.TrimSpace(token) != "" {
		s.sessionToken = token
	} else {
		s.sessionToken = accessToken
	}
	if err := s.openSSE(ctx); err != nil {
		return err
	}
	if _, err := s.rpc(ctx, "initialize", map[string]any{
		"protocolVersion": 1,
		"clientInfo":      map[string]any{"name": "qb2api", "version": "1"},
		"clientCapabilities": map[string]any{
			"fs": map[string]any{"readTextFile": false, "writeTextFile": false},
		},
	}, true); err != nil {
		return err
	}
	cwd, _ := session["cwd"].(string)
	if cwd == "" {
		cwd = "/workspace"
	}
	created, err := s.rpc(ctx, "session/new", map[string]any{
		"cwd": cwd, "mcpServers": []any{},
	}, true)
	if err != nil {
		return err
	}
	sessionID, _ := created["sessionId"].(string)
	if sessionID == "" {
		return activeDayError("session_new_missing")
	}
	if _, err := s.rpc(ctx, "session/set_model", map[string]any{
		"sessionId": sessionID, "modelId": acpModel,
	}, true); err != nil {
		return err
	}
	// The prompt RPC is not answered directly: the turn is complete only when
	// the upstream pushes the end-turn notification over the SSE channel.
	if _, err := s.rpc(ctx, "session/prompt", map[string]any{
		"sessionId": sessionID,
		"prompt":    []any{map[string]any{"type": "text", "text": prompt}},
	}, false); err != nil {
		return err
	}
	return s.waitTurn(ctx)
}

func (s *acpSession) createConversation(ctx context.Context, accessToken, prompt string) (string, error) {
	body := map[string]any{
		"prompt": prompt,
		"model":  acpModel,
		"plugins": []any{
			map[string]any{"name": "weixinpay", "marketplace": "codebuddy-builtin"},
		},
	}
	payload, err := s.requestJSON(ctx, http.MethodPost,
		s.client.baseURL+"/console/as/conversations/", accessToken, body)
	if err != nil {
		return "", err
	}
	data := payload
	if nested, ok := payload["data"].(map[string]any); ok {
		data = nested
	}
	conversationID, _ := data["id"].(string)
	if conversationID == "" {
		conversationID, _ = data["conversationId"].(string)
	}
	if conversationID == "" {
		return "", activeDayError("conversation_missing")
	}
	return conversationID, nil
}

func (s *acpSession) sessionInfo(ctx context.Context, accessToken, conversationID string) (map[string]any, error) {
	payload, err := s.requestJSON(ctx, http.MethodGet,
		s.client.baseURL+"/console/as/conversations/"+conversationID+"/session", accessToken, nil)
	if err != nil {
		return nil, err
	}
	if nested, ok := payload["data"].(map[string]any); ok {
		return nested, nil
	}
	return payload, nil
}

// requestJSON performs one console REST call and unwraps the optional data
// envelope.
func (s *acpSession) requestJSON(ctx context.Context, method, target, accessToken string, body map[string]any) (map[string]any, error) {
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return nil, activeDayError("invalid_json")
		}
		reader = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, target, reader)
	if err != nil {
		return nil, activeDayError("transport:" + typeName(err))
	}
	for key, value := range s.client.conversationHeaders(accessToken) {
		request.Header.Set(key, value)
	}
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := s.client.jsonClient.Do(request)
	if err != nil {
		return nil, activeDayError("transport:" + typeName(err))
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes))
	if err != nil {
		return nil, activeDayError("transport:" + typeName(err))
	}
	if response.StatusCode < 200 || response.StatusCode > 299 {
		return nil, activeDayError(fmt.Sprintf("http:%d", response.StatusCode))
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, activeDayError("invalid_json")
	}
	return payload, nil
}

// acpHeaders are the negotiated headers every ACP call must carry.
func (s *acpSession) acpHeaders() map[string]string {
	headers := map[string]string{
		"Authorization":       "Bearer " + s.sessionToken,
		"Accept":              "application/json, text/event-stream",
		"Content-Type":        "application/json",
		"x-codebuddy-request": "1",
	}
	if s.connectionID != "" {
		headers["Acp-Connection-Id"] = s.connectionID
	}
	if s.acpSessionToken != "" {
		headers["acp-session-token"] = s.acpSessionToken
	}
	return headers
}

// openSSE opens the long-lived event channel and starts draining it. The
// Acp-Connection-Id response header is the routing key for every RPC.
func (s *acpSession) openSSE(ctx context.Context) error {
	streamCtx, cancel := context.WithCancel(ctx)
	request, err := http.NewRequestWithContext(streamCtx, http.MethodGet, s.link, nil)
	if err != nil {
		cancel()
		return activeDayError("transport:" + typeName(err))
	}
	for key, value := range s.acpHeaders() {
		request.Header.Set(key, value)
	}
	response, err := s.client.streamClient.Do(request)
	if err != nil {
		cancel()
		return activeDayError("transport:" + typeName(err))
	}
	if response.StatusCode < 200 || response.StatusCode > 299 {
		response.Body.Close()
		cancel()
		return activeDayError(fmt.Sprintf("acp_sse_http:%d", response.StatusCode))
	}
	connectionID := response.Header.Get("Acp-Connection-Id")
	if connectionID == "" {
		response.Body.Close()
		cancel()
		return activeDayError("acp_connection_missing")
	}
	s.connectionID = connectionID
	s.acpSessionToken = response.Header.Get("acp-session-token")
	s.cancelSSE = cancel
	s.sseBody = response.Body
	s.streamGroup.Add(1)
	go func() {
		defer s.streamGroup.Done()
		s.consume(response.Body)
	}()
	return nil
}

// rpc posts one JSON-RPC message. When wait is false the call returns as soon as
// the request is accepted.
func (s *acpSession) rpc(ctx context.Context, method string, params map[string]any, wait bool) (map[string]any, error) {
	s.mu.Lock()
	requestID := s.nextID + 1
	s.nextID = requestID
	var pending chan rpcResult
	if wait {
		pending = make(chan rpcResult, 1)
		s.pending[requestID] = pending
	}
	s.mu.Unlock()

	message := map[string]any{"jsonrpc": "2.0", "id": requestID, "method": method, "params": params}
	postErr := s.postRPC(ctx, message)
	if !wait {
		return nil, postErr
	}
	defer func() {
		s.mu.Lock()
		delete(s.pending, requestID)
		s.mu.Unlock()
	}()
	if postErr != nil {
		return nil, postErr
	}
	timer := time.NewTimer(s.client.timeout)
	defer timer.Stop()
	select {
	case result := <-pending:
		if result.err != nil {
			return nil, result.err
		}
		return result.result, nil
	case <-timer.C:
		return nil, activeDayError("rpc_timeout")
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// postRPC sends one RPC message and, when the upstream answers with an event
// stream, drains it in the background.
func (s *acpSession) postRPC(ctx context.Context, message map[string]any) error {
	encoded, err := json.Marshal(message)
	if err != nil {
		return activeDayError("invalid_json")
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, s.link, bytes.NewReader(encoded))
	if err != nil {
		return activeDayError("transport:" + typeName(err))
	}
	for key, value := range s.acpHeaders() {
		request.Header.Set(key, value)
	}
	response, err := s.client.streamClient.Do(request)
	if err != nil {
		return activeDayError("transport:" + typeName(err))
	}
	if response.StatusCode < 200 || response.StatusCode > 299 {
		response.Body.Close()
		return activeDayError(fmt.Sprintf("acp_rpc_http:%d", response.StatusCode))
	}
	if strings.Contains(strings.ToLower(response.Header.Get("Content-Type")), "text/event-stream") {
		s.streamGroup.Add(1)
		go func() {
			defer s.streamGroup.Done()
			defer response.Body.Close()
			s.consume(response.Body)
		}()
		return nil
	}
	defer response.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(response.Body, maxResponseBytes))
	if err != nil {
		return activeDayError("transport:" + typeName(err))
	}
	if len(bytes.TrimSpace(raw)) == 0 {
		return nil
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return activeDayError("invalid_json")
	}
	s.dispatch(payload)
	return nil
}

// waitTurn blocks until the end-turn notification arrives.
func (s *acpSession) waitTurn(ctx context.Context) error {
	timer := time.NewTimer(s.client.timeout)
	defer timer.Stop()
	select {
	case <-s.turnDone:
		s.mu.Lock()
		streamErr := s.streamErr
		s.mu.Unlock()
		if streamErr != nil {
			return streamErr
		}
		return nil
	case <-timer.C:
		return activeDayError("rpc_timeout")
	case <-ctx.Done():
		return ctx.Err()
	}
}

// consume parses the SSE framing: a blank line closes one event, and an event's
// payload is the concatenation of its data lines.
func (s *acpSession) consume(body io.Reader) {
	reader := bufio.NewReaderSize(body, 64*1024)
	var eventLines []string
	for {
		line, err := reader.ReadString('\n')
		line = strings.TrimRight(line, "\r\n")
		if line != "" {
			eventLines = append(eventLines, line)
		} else if len(eventLines) > 0 {
			s.dispatch(parseSSEPayload(eventLines))
			eventLines = nil
		}
		if err != nil {
			if len(eventLines) > 0 {
				s.dispatch(parseSSEPayload(eventLines))
			}
			if err != io.EOF && !errors.Is(err, context.Canceled) {
				s.failStream(activeDayError("stream_failed"))
			}
			return
		}
	}
}

// parseSSEPayload mirrors the Python helper: join the data lines, then decode.
func parseSSEPayload(lines []string) map[string]any {
	data := make([]string, 0, len(lines))
	for _, line := range lines {
		if strings.HasPrefix(line, "data:") {
			data = append(data, strings.TrimLeft(line[5:], " \t"))
		}
	}
	joined := strings.Join(data, "\n")
	if strings.TrimSpace(joined) == "" {
		return nil
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(joined), &payload); err != nil {
		return nil
	}
	return payload
}

// dispatch routes one decoded message to a pending RPC and, independently, to
// the end-turn signal.
func (s *acpSession) dispatch(message map[string]any) {
	if message == nil {
		return
	}
	if requestID, ok := numericID(message["id"]); ok {
		s.mu.Lock()
		pending := s.pending[requestID]
		s.mu.Unlock()
		if pending != nil {
			if result, ok := message["result"].(map[string]any); ok {
				select {
				case pending <- rpcResult{result: result}:
				default:
				}
			} else if _, ok := message["error"].(map[string]any); ok {
				select {
				case pending <- rpcResult{err: activeDayError("rpc_error")}:
				default:
				}
			}
		}
	}
	if isEndTurn(message) {
		s.turnOnce.Do(func() { close(s.turnDone) })
	}
}

// numericID accepts a JSON number or an all-digit string, matching the Python
// `str(id).isdigit()` gate.
func numericID(value any) (int, bool) {
	switch typed := value.(type) {
	case float64:
		if typed == float64(int(typed)) {
			return int(typed), true
		}
	case string:
		if typed == "" {
			return 0, false
		}
		total := 0
		for _, char := range typed {
			if char < '0' || char > '9' {
				return 0, false
			}
			total = total*10 + int(char-'0')
		}
		return total, true
	}
	return 0, false
}

// isEndTurn reports whether the message terminates the prompt turn.
func isEndTurn(message map[string]any) bool {
	if method, ok := message["method"].(string); ok {
		if method == "session_end_turn" || method == "session/endTurn" {
			return true
		}
	}
	params, ok := message["params"].(map[string]any)
	if !ok {
		return false
	}
	if update, ok := params["sessionUpdate"].(string); ok {
		if update == "session_end_turn" || update == "session/endTurn" {
			return true
		}
	}
	update, ok := params["update"].(map[string]any)
	if !ok {
		return false
	}
	if kind, ok := update["sessionUpdate"].(string); ok {
		return kind == "session_end_turn" || kind == "session/endTurn"
	}
	return false
}

// failStream records a transport failure and releases every waiter, so a broken
// channel surfaces as an error instead of a timeout.
func (s *acpSession) failStream(err error) {
	s.mu.Lock()
	if s.streamErr == nil {
		s.streamErr = err
	}
	pending := make([]chan rpcResult, 0, len(s.pending))
	for _, channel := range s.pending {
		pending = append(pending, channel)
	}
	s.mu.Unlock()
	for _, channel := range pending {
		select {
		case channel <- rpcResult{err: err}:
		default:
		}
	}
	s.turnOnce.Do(func() { close(s.turnDone) })
}

// disconnect tears the channel down and deletes the conversation. Deletion
// errors are swallowed: the turn already happened, and a failed cleanup must not
// rewrite a success into a failure.
func (s *acpSession) disconnect() {
	if s.cancelSSE != nil {
		s.cancelSSE()
	}
	if s.sseBody != nil {
		s.sseBody.Close()
	}
	s.streamGroup.Wait()
	if s.link != "" && s.connectionID != "" {
		ctx, cancel := context.WithTimeout(context.Background(), minDuration(s.client.timeout, 5*time.Second))
		request, err := http.NewRequestWithContext(ctx, http.MethodDelete, s.link, nil)
		if err == nil {
			for key, value := range s.acpHeaders() {
				request.Header.Set(key, value)
			}
			if response, err := s.client.jsonClient.Do(request); err == nil {
				response.Body.Close()
			}
		}
		cancel()
	}
	s.link = ""
	s.connectionID = ""
	s.acpSessionToken = ""
	s.sessionToken = ""
}

func minDuration(left, right time.Duration) time.Duration {
	if left < right {
		return left
	}
	return right
}
