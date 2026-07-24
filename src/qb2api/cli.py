"""CLI entry point for qoderbuddy2api."""

import argparse
import logging
import os

import uvicorn

from . import __version__


def main():
    parser = argparse.ArgumentParser(description="qoderbuddy2api - OpenAI-compatible proxy for CodeBuddy & Qoder CN")
    parser.add_argument("--version", action="version", version=f"qoderbuddy2api {__version__}")
    parser.add_argument("--host", default=None, help="Host to bind to")
    parser.add_argument("--port", type=int, default=None, help="Port to bind to")
    parser.add_argument("--log-level", default=None, help="Log level")
    parser.add_argument(
        "--mode",
        choices=("control", "worker", "combined"),
        default=None,
        help="Run the persistent Control Plane, Proxy Worker, or compatibility alias",
    )
    args = parser.parse_args()

    from .config import Settings
    settings = Settings.from_env()

    mode = args.mode or os.getenv("QB2API_MODE", "control")
    if mode == "combined":
        logging.getLogger("qb2api.cli").warning(
            "QB2API_MODE=combined is deprecated; using the Control Plane entrypoint"
        )
        mode = "control"
    host = args.host or (settings.control_host if mode == "control" else settings.worker_host)
    port = args.port or (settings.control_port if mode == "control" else settings.worker_port)
    log_level = args.log_level or settings.log_level

    if mode == "control":
        from .control.app import create_control_app

        target = create_control_app()
    elif mode == "worker":
        from .worker.app import app as target
    else:
        from .app import app as target

    uvicorn.run(target, host=host, port=port, log_level=log_level, reload=False)


if __name__ == "__main__":
    main()
