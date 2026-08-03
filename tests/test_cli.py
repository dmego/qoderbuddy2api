"""CLI entrypoint selects the persistent process roles."""

from __future__ import annotations

import sys
from unittest.mock import patch

from qb2api import cli


def test_cli_defaults_to_control_plane(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["qb2api"])

    with patch("qb2api.cli.uvicorn.run") as run:
        cli.main()

    target = run.call_args.args[0]
    assert target.state.role == "control"


def test_cli_worker_mode_selects_worker_app(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["qb2api", "--mode", "worker"])

    with patch("qb2api.cli.uvicorn.run") as run:
        cli.main()

    target = run.call_args.args[0]
    assert target.state.role == "worker"
