package store

import "encoding/json"

func encodeJSON(value any) (string, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return "", err
	}
	return string(raw), nil
}

func decodeMap(raw string) map[string]any {
	if raw == "" {
		return map[string]any{}
	}
	var out map[string]any
	if err := json.Unmarshal([]byte(raw), &out); err != nil || out == nil {
		return map[string]any{}
	}
	return out
}

func decodeAny(raw string) any {
	if raw == "" {
		return nil
	}
	var out any
	if err := json.Unmarshal([]byte(raw), &out); err != nil {
		return nil
	}
	return out
}

func encodeScopes(scopes []string) (string, error) {
	if scopes == nil {
		scopes = []string{"proxy"}
	}
	return encodeJSON(scopes)
}

func decodeScopes(raw string) []string {
	out := []string{}
	_ = json.Unmarshal([]byte(raw), &out)
	if out == nil {
		return []string{}
	}
	return out
}
