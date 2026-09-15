package oauthflow

import "github.com/dmego/qoderbuddy2api/gobackend/internal/config"

// testSettings returns settings with the two endpoints the flows address.
func testSettings() config.Settings {
	return config.Settings{
		CodeBuddyEndpoint:     "https://copilot.tencent.com",
		WorkBuddyIntlEndpoint: "https://www.workbuddy.ai",
	}
}
