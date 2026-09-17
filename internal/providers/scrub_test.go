package providers

import (
	"strings"
	"testing"
)

// rules is a shortened stand-in for the operating rules a coding agent puts in
// its system prompt. They are what stop the model from re-describing the same
// plan forever, so they have to survive the outbound rewrite.
const rules = `Operating rules for this session:
- Investigate before answering; never describe file contents you have not read.
- Batch independent tool calls in one step.
- Keep your reasoning short and never restate a plan you have already written.`

// TestScrubKeepsClientRulesWhenSentinelPresent is the regression test for the
// repetitive-reasoning bug.
//
// The upstream rejects the Claude Code identity line with 400 code=11128, and
// the scrubber used to answer any hit by replacing the *entire* system message
// with "You are a helpful assistant." — discarding the client's operating
// rules with it. With those rules gone the model degenerated into repeating the
// same few sentences thousands of times: measured over repeated live calls,
// reasoning volume went from ~1.1k to ~21-35k characters and finishes flipped
// from a tool call to a buried `length` with no content at all, on both
// providers. Neutralizing only the rejected phrase keeps the rest intact.
func TestScrubKeepsClientRulesWhenSentinelPresent(t *testing.T) {
	sentinel := "You are Claude Code, Anthropic's official CLI for Claude."
	for _, block := range []string{"", "\n\n", " "} {
		system := sentinel + block + rules + block + sentinel

		got := scrubText(system)

		if strings.Contains(got, "Claude Code") {
			t.Fatalf("scrubText left the rejected identity phrase in place: %q", truncateForTest(got))
		}
		if strings.Contains(got, "Anthropic's official CLI for Claude") {
			t.Fatalf("scrubText left the rejected identity phrase in place: %q", truncateForTest(got))
		}
		for _, line := range strings.Split(strings.TrimSpace(rules), "\n") {
			if !strings.Contains(got, line) {
				t.Fatalf("scrubText dropped the client's operating rule %q; result: %q", line, truncateForTest(got))
			}
		}
	}
}

// TestScrubLeavesUnrelatedPromptsAlone pins that a system prompt with no
// rejected phrase is forwarded byte for byte.
func TestScrubLeavesUnrelatedPromptsAlone(t *testing.T) {
	system := "You are an interactive CLI coding agent.\n\n" + rules
	if got := scrubText(system); got != system {
		t.Fatalf("scrubText rewrote a prompt with no sentinel:\n got %q\nwant %q", truncateForTest(got), truncateForTest(system))
	}
}

// TestScrubCoversEverySentinel proves each phrase the filter rejects is
// neutralized, including the ones that only appear inside the full identity
// line, and that the result survives a second pass unchanged.
func TestScrubCoversEverySentinel(t *testing.T) {
	cases := []string{
		"You are Claude Code",
		"You are a Claude agent",
		"Anthropic's official CLI for Claude",
		"Claude Agent SDK",
	}
	for _, sentinel := range cases {
		got := scrubText(sentinel + "\n" + rules)
		for _, other := range cases {
			if strings.Contains(got, other) {
				t.Fatalf("scrubText(%q) still contains %q: %q", sentinel, other, truncateForTest(got))
			}
		}
		if again := scrubText(got); again != got {
			t.Fatalf("scrubText is not idempotent for %q:\n first %q\nsecond %q", sentinel, truncateForTest(got), truncateForTest(again))
		}
	}
}

// TestIntlMessagesPreserveClientSystemPrompt exercises the message builder the
// international provider uses, which is where a scrubbed client prompt actually
// reaches upstream.
func TestIntlMessagesPreserveClientSystemPrompt(t *testing.T) {
	request := decodeRequest(t, `{"model":"deepseek-v4.1-flash","stream":true,"messages":[
		{"role":"system","content":"You are Claude Code, Anthropic's official CLI for Claude.\n`+
		strings.ReplaceAll(rules, "\n", `\n`)+`"},
		{"role":"user","content":"go"}]}`)

	messages := intlMessages(request)
	if len(messages) != 2 {
		t.Fatalf("expected 2 messages, got %d", len(messages))
	}
	content, _ := messages[0]["content"].(string)
	if strings.Contains(content, "Claude Code") {
		t.Fatalf("identity phrase reached the outbound system message: %q", truncateForTest(content))
	}
	if !strings.Contains(content, "never restate a plan you have already written") {
		t.Fatalf("client rules were dropped from the outbound system message: %q", truncateForTest(content))
	}
}

// TestHistoryKeepsRulesButDropsIdentityLine pins the second half of the
// repetitive-reasoning investigation.
//
// The gateway rejects the identity line wherever it appears in the request,
// including inside assistant turns replayed from history. A single reply
// quoting the line poisons the session: every later request fails with 11128
// until the history ages out. So the outbound rewrite must neutralize the line
// in every role while leaving the rest of the turn untouched — a user quoting a
// document, or a tool result naming another agent, must survive verbatim.
func TestHistoryKeepsRulesButDropsIdentityLine(t *testing.T) {
	identity := "You are Claude Code, Anthropic's official CLI for Claude."
	request := decodeRequest(t, `{"model":"deepseek-v4.1-flash","stream":true,"messages":[
		{"role":"system","content":"You are a helpful assistant."},
		{"role":"user","content":"Read the harness output."},
		{"role":"assistant","content":"The constant is \"`+identity+`\", bY = 64000;"},
		{"role":"user","content":"Continue."}]}`)

	messages := make([]map[string]any, 0, len(request.Messages))
	for _, message := range request.Messages {
		messages = append(messages, outboundMessage(message))
	}

	outbound := ""
	for index, message := range messages {
		content, _ := message["content"].(string)
		outbound += content + "\n"
		if strings.Contains(content, "Claude Code") || strings.Contains(content, "Anthropic's official CLI") {
			t.Fatalf("identity line survived scrubbing in message %d (role %v): %q",
				index, message["role"], truncateForTest(content))
		}
	}
	if !strings.Contains(outbound, "bY = 64000;") {
		t.Fatalf("history outside the identity line was rewritten: %q", truncateForTest(outbound))
	}
}

func truncateForTest(text string) string {
	if len(text) <= 300 {
		return text
	}
	return text[:300] + "..."
}
