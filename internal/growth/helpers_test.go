package growth

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// newFakeGrowthServer stands in for the workbuddy.cn growth gateway: it answers
// every request with the {code, data} envelope the client unwraps, returning the
// configured payload for the requested path.
func newFakeGrowthServer(t *testing.T, payloads map[string]map[string]any) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		data, ok := payloads[r.URL.Path]
		if !ok {
			data = map[string]any{}
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"code": 0, "data": data})
	}))
	t.Cleanup(server.Close)
	return server
}
