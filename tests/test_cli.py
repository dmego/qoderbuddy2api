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


def test_cli_combined_mode_is_a_control_plane_compatibility_alias(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["qb2api", "--mode", "combined"])

    with patch("qb2api.cli.uvicorn.run") as run:
        cli.main()

    target = run.call_args.args[0]
    assert target.state.role == "control"
