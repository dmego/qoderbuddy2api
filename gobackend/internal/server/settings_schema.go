package server

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
)

func ensureDir(path string) error {
	if path == "" {
		return nil
	}
	return os.MkdirAll(path, 0o700)
}

// fileSizeAndDigest returns the size and sha256 of a file.
func fileSizeAndDigest(path string) (int64, string, error) {
	handle, err := os.Open(path)
	if err != nil {
		return 0, "", err
	}
	defer handle.Close()
	info, err := handle.Stat()
	if err != nil {
		return 0, "", err
	}
	digest := sha256.New()
	if _, err := io.Copy(digest, handle); err != nil {
		return 0, "", err
	}
	return info.Size(), hex.EncodeToString(digest.Sum(nil)), nil
}

// ---- setting validators ----

func validateBool(value any) error {
	if _, ok := value.(bool); !ok {
		return errors.New("value must be a boolean")
	}
	return nil
}

func validateNonEmptyString(value any) error {
	text, ok := value.(string)
	if !ok || strings.TrimSpace(text) == "" {
		return errors.New("value must be a non-empty string")
	}
	return nil
}

// validateClock checks an HH:MM schedule time.
func validateClock(value any) error {
	text, ok := value.(string)
	if !ok {
		return errors.New("value must be a string")
	}
	parts := strings.Split(strings.TrimSpace(text), ":")
	if len(parts) != 2 {
		return fmt.Errorf("invalid time: %s", text)
	}
	hour, err := strconv.Atoi(parts[0])
	if err != nil || hour < 0 || hour > 23 {
		return fmt.Errorf("invalid time: %s", text)
	}
	minute, err := strconv.Atoi(parts[1])
	if err != nil || minute < 0 || minute > 59 {
		return fmt.Errorf("invalid time: %s", text)
	}
	return nil
}

func validateRedeemTier(value any) error {
	text, ok := value.(string)
	if !ok {
		return errors.New("growth.redeem_tier must be 7d, 14d, 28d, or off")
	}
	switch text {
	case "7d", "14d", "28d", "off":
		return nil
	}
	return errors.New("growth.redeem_tier must be 7d, 14d, 28d, or off")
}

// rangeValidator builds an integer range check with the Python message text.
func rangeValidator(low, high int, message string) func(any) error {
	return func(value any) error {
		number, ok := numeric(value)
		if !ok {
			return errors.New(message)
		}
		if number < low || number > high {
			return errors.New(message)
		}
		return nil
	}
}

// numeric coerces a JSON number (float64) or string into an int.
func numeric(value any) (int, bool) {
	switch typed := value.(type) {
	case float64:
		return int(typed), true
	case int:
		return typed, true
	case string:
		parsed, err := strconv.Atoi(strings.TrimSpace(typed))
		if err != nil {
			return 0, false
		}
		return parsed, true
	}
	return 0, false
}

// boolValue coerces a JSON boolean, defaulting when absent.
func boolValue(value any, fallback bool) bool {
	if flag, ok := value.(bool); ok {
		return flag
	}
	return fallback
}

// stringValue coerces a JSON string, defaulting when absent.
func stringValue(value any, fallback string) string {
	if text, ok := value.(string); ok {
		return text
	}
	return fallback
}
