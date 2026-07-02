# Changelog

## [1.0.0] — 2026-07-01

### Added

- Initial release: qoderbuddy2api
- CodeBuddy provider — direct HTTP API, 14 models
- Qoder CN provider — COSY protocol direct HTTP, 9 models
- Load-balanced provider with round-robin + 30s failover cooldown
- Multiple API key support via comma-separated tokens
- OpenAI-compatible `/v1/chat/completions` (streaming + non-streaming)
- `/v1/models`, `/health`, `/version` endpoints
- Ollama-compatible `/api/tags`, `/api/show` stubs
- Tool calling support for both providers
- Reasoning content aggregation
- Optional API key authentication middleware
- Request logging (console + JSONL file)
- Docker / docker-compose deployment
- CLI via `qb2api` command
- Python 3.11+ support
