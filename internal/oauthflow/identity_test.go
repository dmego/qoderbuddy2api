package oauthflow

import "testing"

// credentialIdentity must survive whatever the provider hands back: the
// identity gates de-duplication, and a panic or a bogus identity here would
// either crash the import or merge two accounts.
func TestCredentialIdentityBoundaries(t *testing.T) {
	cases := []struct {
		name        string
		payload     map[string]any
		wantID      string
		wantDisplay string
	}{
		{
			name:        "keycloak claims",
			payload:     map[string]any{"access_token": testToken(map[string]any{"sub": "abc", "preferred_username": "13126806311"})},
			wantID:      "abc",
			wantDisplay: "13126806311",
		},
		{
			name:        "email wins over handle",
			payload:     map[string]any{"access_token": testToken(map[string]any{"sub": "abc", "email": "a@b.com", "preferred_username": "handle"})},
			wantID:      "abc",
			wantDisplay: "a@b.com",
		},
		{
			name:    "opaque token names nothing",
			payload: map[string]any{"access_token": "not-a-jwt"},
		},
		{
			name:    "payload is not json",
			payload: map[string]any{"access_token": "header.bm90LWpzb24.signature"},
		},
		{
			name:        "a non-string sub is not an identity, but the display name still is",
			payload:     map[string]any{"access_token": testToken(map[string]any{"sub": 42, "preferred_username": "handle"})},
			wantDisplay: "handle",
		},
		{
			name:    "label characters outside the console's set are refused",
			payload: map[string]any{"access_token": testToken(map[string]any{"sub": "abc", "preferred_username": "bad\nname"})},
			wantID:  "abc",
		},
		{
			name:    "missing token",
			payload: map[string]any{},
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			identity, display := credentialIdentity(testCase.payload)
			if identity != testCase.wantID {
				t.Fatalf("identity = %q, want %q", identity, testCase.wantID)
			}
			if display != testCase.wantDisplay {
				t.Fatalf("display = %q, want %q", display, testCase.wantDisplay)
			}
		})
	}
}

// The console's own default names must be treated as "no label chosen", so an
// unlabelled import takes the identity instead of storing a shared placeholder.
func TestPlaceholderLabels(t *testing.T) {
	for _, label := range []string{"", "  ", "WorkBuddy OAuth", "WorkBuddy 国际版 OAuth"} {
		if !isPlaceholderLabel(label) {
			t.Fatalf("%q must count as a placeholder", label)
		}
	}
	for _, label := range []string{"主账号", "user@example.com", "WorkBuddy OAuth 2"} {
		if isPlaceholderLabel(label) {
			t.Fatalf("%q carries operator intent and must be kept", label)
		}
	}
}
