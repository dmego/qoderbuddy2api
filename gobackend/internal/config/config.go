// Package config loads runtime settings from the environment.
//
// Field names and environment variable names mirror the Python original so the
// existing .env files keep working unchanged.
package config

import (
	"net"
	"os"
	"strconv"
	"strings"
)

type Settings struct {
	// Server
	Host        string
	Port        int
	ControlHost string
	ControlPort int
	WorkerHost  string
	WorkerPort  int
	LogLevel    string

	// Auth
	ProxyAPIKey string
	AdminKey    string

	// Storage
	DataDir           string
	LogDir            string
	CredentialKey     string
	ModelConfigPath   string
	AdminUIEnabled    bool
	AdminUIPath       string
	AdminCookieSecure string
	AdminSessionTTL   int // hours
	AdminSessionIdle  int // minutes

	// Providers (only the two WorkBuddy deployments survive the rewrite)
	CodeBuddyEndpoint            string
	CodeBuddyDefaultReasoning    string
	WorkBuddyIntlEndpoint        string
	WorkBuddyIntlReasoning       string
	WorkBuddyIntlCreditsPath     string
	CodeBuddyCheckinBase         string
	CodeBuddyCheckinStatusPath   string
	CodeBuddyCheckinClaimPath    string
	CodeBuddyCreditsPath         string
	CodeBuddyCheckinStatusMethod string
	CodeBuddyCheckinClaimMethod  string

	LegacyCodeBuddyTokens []string
	LegacyIntlTokens      []string

	// Check-in
	CheckinEnabled      bool
	CheckinAt           string
	CheckinTimezone     string
	CheckinCatchUp      bool
	CheckinCatchUpHours int
	CheckinJitterMin    int
	CheckinJitterMax    int
	CheckinTimeout      int
	CheckinRetryLimit   int
	CheckinWorkbuddy    bool

	// Check-in claim endpoint (domestic WorkBuddy only after the rewrite)
	CheckinStatusPath  string
	CheckinClaimPath   string
	CheckinClaimMethod string

	// Observability
	MetricsEnabled          bool
	MetricsIntervalSeconds  int
	MetricsHistoryRetention int
	UsageRollupInterval     int
	UsageDetailRetention    int
	StreamReasoning         bool
	LogRequests             bool
	CredentialRefresh       bool
	CredentialRefreshEvery  int
	CredentialRefreshLead   int

	// Growth centre
	GrowthSchedulerEnabled  bool
	GrowthSchedulerInterval int
	GrowthAutoTasks         bool
	GrowthAutoLottery       bool
	GrowthAutoTravel        bool
	GrowthAutoRedeem        bool
	GrowthRedeemTier        string
	GrowthAutoBuddyOpen     bool
	GrowthAutoActiveDay     bool
	GrowthActiveDayAttempts int

	// Write coalescing (Go rewrite): 0 disables batching.
	EventFlushMillis int
	EventFlushMax    int
}

func Load() Settings {
	s := Settings{
		Host:        env("QB2API_HOST", "0.0.0.0"),
		Port:        envInt("QB2API_PORT", 9999),
		ControlHost: env("QB2API_CONTROL_HOST", "127.0.0.1"),
		ControlPort: envInt("QB2API_CONTROL_PORT", 9999),
		WorkerHost:  env("QB2API_WORKER_HOST", "127.0.0.1"),
		WorkerPort:  envInt("QB2API_WORKER_PORT", 10001),
		LogLevel:    env("QB2API_LOG_LEVEL", "info"),

		ProxyAPIKey: os.Getenv("QB2API_PROXY_API_KEY"),
		AdminKey:    os.Getenv("QB2API_ADMIN_KEY"),

		DataDir:           env("QB2API_DATA_DIR", "./data"),
		LogDir:            env("QB2API_LOG_DIR", "./logs"),
		CredentialKey:     os.Getenv("QB2API_CREDENTIAL_KEY"),
		ModelConfigPath:   env("QB2API_MODEL_CONFIG", "./config/models.json"),
		AdminUIEnabled:    envBool("QB2API_ADMIN_UI_ENABLED", false),
		AdminUIPath:       env("QB2API_ADMIN_UI_PATH", "/admin"),
		AdminCookieSecure: strings.ToLower(env("QB2API_ADMIN_COOKIE_SECURE", "auto")),
		AdminSessionTTL:   envInt("QB2API_ADMIN_SESSION_TTL_HOURS", 12),
		AdminSessionIdle:  envInt("QB2API_ADMIN_SESSION_IDLE_MINUTES", 60),

		CodeBuddyEndpoint:         env("CODEBUDDY_ENDPOINT", "https://copilot.tencent.com"),
		CodeBuddyDefaultReasoning: strings.ToLower(strings.TrimSpace(env("QB2API_CODEBUDDY_DEFAULT_REASONING_EFFORT", "max"))),
		WorkBuddyIntlEndpoint:     env("WORKBUDDY_INTL_ENDPOINT", "https://www.workbuddy.ai"),
		WorkBuddyIntlReasoning:    strings.ToLower(strings.TrimSpace(env("QB2API_INTL_DEFAULT_REASONING_EFFORT", "low"))),
		WorkBuddyIntlCreditsPath:  env("WORKBUDDY_INTL_CREDITS_PATH", "/billing/meter/get-user-resource"),

		CodeBuddyCheckinBase:         env("CODEBUDDY_CHECKIN_BASE", "https://www.workbuddy.cn"),
		CodeBuddyCheckinStatusPath:   env("CODEBUDDY_CHECKIN_STATUS_PATH", "/billing/meter/checkin-status"),
		CodeBuddyCheckinClaimPath:    env("CODEBUDDY_CHECKIN_CLAIM_PATH", "/billing/meter/daily-checkin"),
		CodeBuddyCreditsPath:         env("CODEBUDDY_CREDITS_PATH", "/billing/meter/get-user-resource"),
		CodeBuddyCheckinStatusMethod: strings.ToUpper(strings.TrimSpace(os.Getenv("CODEBUDDY_CHECKIN_STATUS_METHOD"))),
		CodeBuddyCheckinClaimMethod:  upperOr(env("CODEBUDDY_CHECKIN_CLAIM_METHOD", "POST"), "POST"),

		LegacyCodeBuddyTokens: splitTokens(os.Getenv("CODEBUDDY_TOKEN")),
		LegacyIntlTokens:      splitTokens(os.Getenv("WORKBUDDY_INTL_TOKEN")),

		CheckinEnabled:      envBool("CHECKIN_ENABLED", false),
		CheckinAt:           env("CHECKIN_AT", "00:10"),
		CheckinTimezone:     env("CHECKIN_TIMEZONE", "Asia/Shanghai"),
		CheckinCatchUp:      envBool("CHECKIN_CATCH_UP", true),
		CheckinCatchUpHours: envInt("CHECKIN_CATCH_UP_WINDOW_HOURS", 6),
		CheckinJitterMin:    envInt("CHECKIN_JITTER_MIN_SECONDS", 3),
		CheckinJitterMax:    envInt("CHECKIN_JITTER_MAX_SECONDS", 10),
		CheckinTimeout:      envInt("CHECKIN_REQUEST_TIMEOUT_SECONDS", 15),
		CheckinRetryLimit:   envInt("CHECKIN_RETRY_LIMIT", 2),
		CheckinWorkbuddy:    envBool("CODEBUDDY_CHECKIN_ENABLED", false),

		CheckinStatusPath:  env("CODEBUDDY_CHECKIN_STATUS_PATH", "/billing/meter/checkin-status"),
		CheckinClaimPath:   env("CODEBUDDY_CHECKIN_CLAIM_PATH", "/billing/meter/daily-checkin"),
		CheckinClaimMethod: upperOr(env("CODEBUDDY_CHECKIN_CLAIM_METHOD", "POST"), "POST"),

		MetricsEnabled:          envBool("QB2API_METRICS_ENABLED", true),
		MetricsIntervalSeconds:  envInt("QB2API_METRICS_INTERVAL_SECONDS", 900),
		MetricsHistoryRetention: envInt("QB2API_METRICS_HISTORY_RETENTION_DAYS", 90),
		UsageRollupInterval:     envInt("QB2API_USAGE_ROLLUP_INTERVAL_SECONDS", 60),
		UsageDetailRetention:    envInt("QB2API_USAGE_DETAIL_RETENTION_DAYS", 90),
		StreamReasoning:         envBool("QB2API_STREAM_REASONING", true),
		LogRequests:             envBool("QB2API_LOG_REQUESTS", true),
		CredentialRefresh:       envBool("QB2API_CREDENTIAL_REFRESH_ENABLED", true),
		CredentialRefreshEvery:  envInt("QB2API_CREDENTIAL_REFRESH_INTERVAL_SECONDS", 900),
		CredentialRefreshLead:   envInt("QB2API_CREDENTIAL_REFRESH_LEAD_SECONDS", 1800),

		GrowthSchedulerEnabled:  envBool("GROWTH_SCHEDULER_ENABLED", true),
		GrowthSchedulerInterval: envInt("GROWTH_SCHEDULER_INTERVAL_SECONDS", 1800),
		GrowthAutoTasks:         envBool("GROWTH_AUTO_TASKS", true),
		GrowthAutoLottery:       envBool("GROWTH_AUTO_LOTTERY", true),
		GrowthAutoTravel:        envBool("GROWTH_AUTO_TRAVEL", true),
		GrowthAutoRedeem:        envBool("GROWTH_AUTO_REDEEM", true),
		GrowthRedeemTier:        env("GROWTH_REDEEM_TIER", "14d"),
		GrowthAutoBuddyOpen:     envBool("GROWTH_AUTO_BUDDY_OPEN", false),
		GrowthAutoActiveDay:     envBool("GROWTH_AUTO_ACTIVE_DAY", true),
		GrowthActiveDayAttempts: envInt("GROWTH_ACTIVE_DAY_CONFIRM_ATTEMPTS", 3),

		EventFlushMillis: envInt("QB2API_EVENT_FLUSH_MILLIS", 250),
		EventFlushMax:    envInt("QB2API_EVENT_FLUSH_MAX", 200),
	}
	if s.CheckinEnabled == false {
		// CHECKIN_ENABLED unset historically derived from the per-provider flags.
		if _, ok := os.LookupEnv("CHECKIN_ENABLED"); !ok && s.CheckinWorkbuddy {
			s.CheckinEnabled = true
		}
	}
	return s
}

func (s Settings) DBPath() string { return s.DataDir + "/qb2api.sqlite3" }

func (s Settings) Addr() string { return net.JoinHostPort(s.Host, strconv.Itoa(s.Port)) }

func (s Settings) AdminCookieSecureMode() string {
	mode := strings.ToLower(strings.TrimSpace(s.AdminCookieSecure))
	if mode == "" {
		return "auto"
	}
	return mode
}

func env(key, fallback string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return fallback
}

func envInt(key string, fallback int) int {
	raw, ok := os.LookupEnv(key)
	if !ok || strings.TrimSpace(raw) == "" {
		return fallback
	}
	value, err := strconv.Atoi(strings.TrimSpace(raw))
	if err != nil {
		return fallback
	}
	return value
}

func envBool(key string, fallback bool) bool {
	raw, ok := os.LookupEnv(key)
	if !ok {
		return fallback
	}
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off", "":
		return false
	}
	return fallback
}

// splitTokens accepts either a comma-separated list or the JSON array form the
// legacy PATCH /api/config endpoint used to write.
func splitTokens(raw string) []string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil
	}
	if strings.HasPrefix(raw, "[") {
		trimmed := strings.Trim(raw, "[]")
		var out []string
		for _, part := range strings.Split(trimmed, ",") {
			part = strings.Trim(strings.TrimSpace(part), `"`)
			if part != "" {
				out = append(out, part)
			}
		}
		return out
	}
	var out []string
	for _, part := range strings.Split(raw, ",") {
		if part = strings.TrimSpace(part); part != "" {
			out = append(out, part)
		}
	}
	return out
}

func upperOr(value, fallback string) string {
	value = strings.ToUpper(strings.TrimSpace(value))
	if value == "" {
		return fallback
	}
	return value
}
