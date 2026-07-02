# Contributing to qoderbuddy2api

Thanks for your interest in contributing! Here's how to get started.

## Development Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running Tests

```bash
pytest tests/ -v
```

## Code Style

- Python 3.11+
- Format with `ruff format`
- Lint with `ruff check`
- Line length: 120 characters
- Comments in English

## Project Structure

```
src/qb2api/
├── __init__.py          # Version info
├── __main__.py          # python -m entry point
├── app.py               # FastAPI application, routes, middleware
├── cli.py               # CLI entry point
├── config.py            # Environment configuration
├── logger.py            # Request logging
├── models.py            # Model definitions and capabilities
├── openai.py            # OpenAI-compatible data models
├── sse.py               # SSE parsing and stream aggregation
└── providers/
    ├── __init__.py
    ├── base.py          # Abstract Provider base class
    ├── codebuddy.py     # CodeBuddy HTTP API provider
    ├── lb.py            # Load-balanced provider wrapper
    └── qoder.py         # Qoder CN COSY protocol provider
```

## Adding a New Provider

1. Create `src/qb2api/providers/<name>.py`
2. Subclass `Provider` and implement `complete()`, `stream()`, `close()`
3. Add models to `DEFAULT_<NAME>_MODELS` in `models.py`
4. Register in `app.py` lifespan

## Pull Request Checklist

- [ ] Tests pass (`pytest tests/ -v`)
- [ ] Code is formatted (`ruff format`)
- [ ] New provider has tests
- [ ] Comments are in English
