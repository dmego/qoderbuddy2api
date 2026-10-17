package server

import (
	"strings"
)

// refreshFailure kinds. The distinction is the whole point of this file: the
// same upstream endpoint answers both a session that can never come back and a
// transient hiccup, and the two need opposite treatment.
type refreshFailure int

const (
	// refreshTransient is a failure that may succeed on the next attempt: a
	// network fault, a 5xx, or the bare "12153 refresh token failed" body that
	// upstream is measured to emit intermittently for a refresh token that
	// works moments later (2026-10-06).
	refreshTransient refreshFailure = iota
	// refreshSessionDead means the Keycloak session the refresh token belongs
	// to no longer exists on the server. A refresh can never revive it; only a
	// fresh sign-in can.
	refreshSessionDead
	// refreshRejected means the credential cannot be refreshed for a reason the
	// caller must fix (no refresh token stored, malformed payload).
	refreshRejected
)

func (k refreshFailure) String() string {
	switch k {
	case refreshSessionDead:
		return "session_dead"
	case refreshRejected:
		return "rejected"
	}
	return "transient"
}

// sessionDeadMarkers are the upstream texts that prove the session is gone.
//
// Measured 2026-10-17: upstream starts answering these after an administrator
// revokes a session server-side. The tokens themselves still look valid — the
// JWT keeps its 28-day exp — so nothing about the credential's own metadata
// predicts this. Three of four accounts were revoked at the same instant while
// the fourth survived, which rules out anything the client did.
//
// The bare "12153" code is deliberately NOT a marker: the same code arrives
// for a transient fault (see refreshTransient), and treating it as death is
// what would disable an account that would have recovered.
var sessionDeadMarkers = []string{
	// Keycloak's KeycloakError for a refresh token whose session was revoked
	// and then recreated: the token predates the new session.
	"Refresh token issued before the user session started",
	// The offline session backing the refresh token was deleted outright.
	"Offline user session not found",
}

// classifyRefreshFailure sorts one refresh error.
func classifyRefreshFailure(err error) refreshFailure {
	if err == nil {
		return refreshTransient
	}
	message := err.Error()
	for _, marker := range sessionDeadMarkers {
		if strings.Contains(message, marker) {
			return refreshSessionDead
		}
	}
	switch {
	case strings.Contains(message, "no refresh token"),
		strings.Contains(message, "auth_no_token"),
		strings.Contains(message, "auth_invalid_json"):
		return refreshRejected
	}
	return refreshTransient
}
