"""CLI entry point for qoderbuddy2api."""

import argparse
import uvicorn

from . import __version__


def main():
    parser = argparse.ArgumentParser(description="qoderbuddy2api - OpenAI-compatible proxy for CodeBuddy & Qoder CN")
    parser.add_argument("--version", action="version", version=f"qoderbuddy2api {__version__}")
    parser.add_argument("--host", default=None, help="Host to bind to")
    parser.add_argument("--port", type=int, default=None, help="Port to bind to")
    parser.add_argument("--log-level", default=None, help="Log level")
    args = parser.parse_args()

    from .config import Settings
    settings = Settings.from_env()

    host = args.host or settings.host
    port = args.port or settings.port
    log_level = args.log_level or settings.log_level

    uvicorn.run("qb2api.app:app", host=host, port=port, log_level=log_level, reload=False)


if __name__ == "__main__":
    main()
