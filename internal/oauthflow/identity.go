package oauthflow

import (
	"encoding/base64"
	"encoding/json"
	"strings"
	"unicode"
)

// identityLabelMax is the label bound the console and the pre-rewrite build
// enforced (LABEL_RE: 1..64 characters of [\w .@+-]).
const identityLabelMax = 64

// credentialIdentity reads the upstream account a signed-in access token
// belongs to, plus the display identity to fall back on when the operator did
// not name the import.
//
// Both deployments sign the operator in through Keycloak on their own realm
// (…/auth/realms/copilot), so the token's `sub` claim is the one value that
// stays the same across logins and is therefore the only sound
// de-duplication key. The display identity is the first usable of
// email → preferred_username → name, which is what the previous build's
// intl_identity resolved.
//
// An opaque or malformed token yields ("", ""). Callers must then keep the
// operator's own label and must NOT treat that label as an identity: the label
// is operator-facing text with a constant default for unlabelled logins, so
// hashing it made every later login resolve to the first account it created —
// the second WorkBuddy login imported no new account and overwrote the first
// one's credential.
func credentialIdentity(payload map[string]any) (identity, display string) {
	token, _ := payload["access_token"].(string)
	claims := jwtClaims(token)
	identity = usableClaim(claims["sub"], false)
	for _, claim := range []string{"email", "preferred_username", "name"} {
		if candidate := usableClaim(claims[claim], true); candidate != "" {
			return identity, candidate
		}
	}
	return identity, ""
}

// jwtClaims decodes a JWT's payload segment without verifying its signature:
// the claims are read for identity only, and the provider stays the authority
// on whether the token is genuine. Returns nil when the token is opaque.
func jwtClaims(token string) map[string]any {
	segments := strings.Split(token, ".")
	if len(segments) != 3 {
		return nil
	}
	raw, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(segments[1], "="))
	if err != nil {
		return nil
	}
	var claims map[string]any
	if err := json.Unmarshal(raw, &claims); err != nil {
		return nil
	}
	return claims
}

// usableClaim renders one claim value as text, bounded and optionally checked
// against the label character set so a derived label cannot smuggle control
// characters or an unbounded string into storage.
func usableClaim(value any, asLabel bool) string {
	text, ok := value.(string)
	if !ok {
		return ""
	}
	text = strings.TrimSpace(text)
	if text == "" || len(text) > identityLabelMax {
		return ""
	}
	if !asLabel {
		return text
	}
	for _, runeValue := range text {
		if !labelRune(runeValue) {
			return ""
		}
	}
	return text
}

// labelRune matches the console's accepted label characters.
func labelRune(value rune) bool {
	if unicode.IsLetter(value) || unicode.IsDigit(value) {
		return true
	}
	switch value {
	case '_', ' ', '.', '@', '+', '-':
		return true
	}
	return false
}
