"""Executable fresh and migrated installation smoke contracts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "script",
    ("scripts/smoke_fresh_install.sh", "scripts/smoke_migrated_install.sh"),
)
def test_install_smoke_script(script: str) -> None:
    environment = dict(os.environ, PYTHON_BIN=sys.executable, QB2API_SMOKE_TIMEOUT_SECONDS="20")
    result = subprocess.run(
        ["bash", script], cwd=ROOT, env=environment, text=True, capture_output=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
