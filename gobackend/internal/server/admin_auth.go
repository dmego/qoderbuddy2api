package server

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/dmego/qoderbuddy2api/gobackend/internal/config"
	"github.com/dmego/qoderbuddy2api/gobackend/internal/store"
)

// Admin session cookie contract, matching admin/auth.py.
const (
	AdminCookieName = "qb2api_admin_session"
	AdminCookiePath = "/api/admin"
	CSRFHeaderName  = "X-CSRF-Token"
)

// AdminAuth owns admin session verification, CSRF checks and login throttling.
type AdminAuth struct {
	settings config.Settings
	db       *store.DB
	limiter  *loginLimiter
}

// NewAdminAuth builds the auth helper.
func NewAdminAuth(settings config.Settings, db *store.DB) *AdminAuth {
	return &AdminAuth{settings: settings, db: db, limiter: newLoginLimiter(5, 5*time.Minute, 15*time.Minute)}
}

// ErrCookiePolicy reports a request context the Secure policy rejects.
var ErrCookiePolicy = errors.New("admin session requires HTTPS for non-loopback (cookie secure=auto)")

// VerifyRequest authorizes one admin API call.
//
// Two credentials are accepted, matching the Python middleware: an admin session
// cookie (same-origin console) or a bearer admin key (scripts, /api/admin
// consumers). Mutating methods additionally require the CSRF header when the
// request is cookie-authenticated.
func (a *AdminAuth) VerifyRequest(r *http.Request) error {
	if token := bearerToken(r); token != "" && a.verifyAdminKey(token) {
		return nil
	}
	cookie, err := r.Cookie(AdminCookieName)
	if err != nil || cookie.Value == "" {
		return errUnauthorized
	}
	session, err := a.db.GetSession(r.Context(), hashToken(cookie.Value))
	if err != nil {
		return errUnauthorized
	}
	now := time.Now().UTC()
	expiresAt, ok := store.ParseISO(session.ExpiresAt)
	if !ok || now.After(expiresAt) {
		return errUnauthorized
	}
	if session.RevokedAt != nil && *session.RevokedAt != "" {
		return errUnauthorized
	}
	lastSeen, ok := store.ParseISO(session.LastSeenAt)
	if ok && a.settings.AdminSessionIdle > 0 && now.Sub(lastSeen) > time.Duration(a.settings.AdminSessionIdle)*time.Minute {
		return errUnauthorized
	}
	if isMutating(r.Method) {
		presented := r.Header.Get(CSRFHeaderName)
		if presented == "" || hashToken(presented) != session.CSRFHash {
			return errCSRF
		}
	}
	// Sliding expiry: refresh the idle window on every authenticated call.
	ttl := time.Duration(maxInt(a.settings.AdminSessionTTL, 1)) * time.Hour
	if err := a.db.TouchSession(r.Context(), session.SessionHash, store.NowISO(), store.FormatISO(now.Add(ttl))); err != nil {
		return err
	}
	return nil
}

var (
	errUnauthorized = errors.New("invalid or missing admin credentials")
	errCSRF         = errors.New("invalid CSRF token")
)

// Login validates the admin key and creates a session.
func (a *AdminAuth) Login(ctx context.Context, r *http.Request, presented string) (sessionToken, csrfToken string, err error) {
	ip := clientIP(r)
	if a.limiter.locked(ip) {
		return "", "", errRateLimited
	}
	if !a.verifyAdminKey(presented) {
		a.limiter.recordFailure(ip)
		return "", "", errUnauthorized
	}
	if err := a.checkCookiePolicy(r); err != nil {
		return "", "", err
	}
	a.limiter.recordSuccess(ip)

	sessionToken = randomToken(32)
	csrfToken = randomToken(32)
	now := time.Now().UTC()
	ttl := time.Duration(maxInt(a.settings.AdminSessionTTL, 1)) * time.Hour
	session := store.Session{
		SessionHash: hashToken(sessionToken),
		CSRFHash:    hashToken(csrfToken),
		CreatedAt:   store.FormatISO(now),
		LastSeenAt:  store.FormatISO(now),
		ExpiresAt:   store.FormatISO(now.Add(ttl)),
	}
	if err := a.db.CreateSession(ctx, session); err != nil {
		return "", "", err
	}
	return sessionToken, csrfToken, nil
}

var errRateLimited = errors.New("login_rate_limited")

// Logout revokes the session carried by the request cookie.
func (a *AdminAuth) Logout(ctx context.Context, r *http.Request) error {
	cookie, err := r.Cookie(AdminCookieName)
	if err != nil || cookie.Value == "" {
		return nil
	}
	return a.db.RevokeSession(ctx, hashToken(cookie.Value))
}

// checkCookiePolicy rejects a login the Secure policy cannot serve, matching
// resolve_cookie_secure's error for non-loopback HTTP under mode "auto".
func (a *AdminAuth) checkCookiePolicy(r *http.Request) error {
	_, err := a.cookieSecure(r)
	return err
}

// cookieSecure resolves the Secure flag for the session cookie.
func (a *AdminAuth) cookieSecure(r *http.Request) (bool, error) {
	https := a.isHTTPS(r)
	loopback := isLoopback(r)
	switch a.settings.AdminCookieSecureMode() {
	case "true":
		if !https {
			return false, errors.New("admin cookie secure=true requires HTTPS")
		}
		return true, nil
	case "false":
		// Explicit operator override for a trusted LAN/Tailscale HTTP setup.
		return false, nil
	default:
		if https {
			return true, nil
		}
		if loopback {
			return false, nil
		}
		return false, ErrCookiePolicy
	}
}

func (a *AdminAuth) isHTTPS(r *http.Request) bool {
	if r.TLS != nil {
		return true
	}
	return strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https")
}

// SetSessionCookie writes the session cookie with the resolved attributes.
func (a *AdminAuth) SetSessionCookie(w http.ResponseWriter, r *http.Request, token string) error {
	secure, err := a.cookieSecure(r)
	if err != nil {
		return err
	}
	ttl := time.Duration(maxInt(a.settings.AdminSessionTTL, 1)) * time.Hour
	http.SetCookie(w, &http.Cookie{
		Name:     AdminCookieName,
		Value:    token,
		Path:     AdminCookiePath,
		MaxAge:   int(ttl.Seconds()),
		HttpOnly: true,
		Secure:   secure,
		SameSite: http.SameSiteLaxMode,
	})
	return nil
}

// ClearSessionCookie expires the session cookie.
func (a *AdminAuth) ClearSessionCookie(w http.ResponseWriter, r *http.Request) {
	secure, _ := a.cookieSecure(r)
	http.SetCookie(w, &http.Cookie{
		Name:     AdminCookieName,
		Value:    "",
		Path:     AdminCookiePath,
		MaxAge:   -1,
		HttpOnly: true,
		Secure:   secure,
		SameSite: http.SameSiteLaxMode,
	})
}

func (a *AdminAuth) verifyAdminKey(presented string) bool {
	expected := a.settings.AdminKey
	if expected == "" || presented == "" {
		return false
	}
	return constantTimeEqual(presented, expected)
}

// requireAdmin guards a handler, writing the canonical 401/403 response.
func (a *AdminAuth) requireAdmin(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if err := a.VerifyRequest(r); err != nil {
			status := http.StatusUnauthorized
			detail := "invalid_admin_key"
			if errors.Is(err, errCSRF) {
				status = http.StatusForbidden
				detail = "invalid_csrf_token"
			}
			writeError(w, status, detail)
			return
		}
		next(w, r)
	}
}

func isMutating(method string) bool {
	switch method {
	case http.MethodPost, http.MethodPatch, http.MethodPut, http.MethodDelete:
		return true
	}
	return false
}

func bearerToken(r *http.Request) string {
	header := r.Header.Get("Authorization")
	if !strings.HasPrefix(header, "Bearer ") {
		return ""
	}
	return strings.TrimSpace(strings.TrimPrefix(header, "Bearer "))
}

func randomToken(bytes int) string {
	raw := make([]byte, bytes)
	if _, err := rand.Read(raw); err != nil {
		return ""
	}
	return base64.RawURLEncoding.EncodeToString(raw)
}

func constantTimeEqual(left, right string) bool {
	if len(left) != len(right) {
		// Still hash to keep the comparison time independent of the match
		// position; differing lengths are already public information.
		leftDigest := sha256.Sum256([]byte(left))
		rightDigest := sha256.Sum256([]byte(right))
		return leftDigest == rightDigest
	}
	var diff byte
	for index := 0; index < len(left); index++ {
		diff |= left[index] ^ right[index]
	}
	return diff == 0
}

func clientIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

// loginLimiter implements the 5-failures-then-15-minute lockout per IP.
type loginLimiter struct {
	mu          sync.Mutex
	maxFailures int
	window      time.Duration
	lockFor     time.Duration
	failures    map[string][]time.Time
	lockedUntil map[string]time.Time
}

func newLoginLimiter(maxFailures int, window, lockFor time.Duration) *loginLimiter {
	return &loginLimiter{
		maxFailures: maxFailures,
		window:      window,
		lockFor:     lockFor,
		failures:    map[string][]time.Time{},
		lockedUntil: map[string]time.Time{},
	}
}

func (l *loginLimiter) locked(ip string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	until, ok := l.lockedUntil[ip]
	if !ok {
		return false
	}
	if time.Now().Before(until) {
		return true
	}
	delete(l.lockedUntil, ip)
	return false
}

func (l *loginLimiter) recordFailure(ip string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	cutoff := time.Now().Add(-l.window)
	kept := []time.Time{}
	for _, stamp := range l.failures[ip] {
		if stamp.After(cutoff) {
			kept = append(kept, stamp)
		}
	}
	kept = append(kept, time.Now())
	l.failures[ip] = kept
	if len(kept) >= l.maxFailures {
		l.lockedUntil[ip] = time.Now().Add(l.lockFor)
		l.failures[ip] = nil
	}
}

func (l *loginLimiter) recordSuccess(ip string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	delete(l.failures, ip)
	delete(l.lockedUntil, ip)
}

func maxInt(value, fallback int) int {
	if value <= 0 {
		return fallback
	}
	return value
}

// hashTokenForCSRF is the CSRF hash helper used by the session route.
func hashTokenForCSRF(token string) string { return hashToken(token) }

var _ = hex.EncodeToString
