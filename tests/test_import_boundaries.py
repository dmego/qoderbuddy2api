"""Import boundaries must not initialize mutually dependent applications."""

from __future__ import annotations

import subprocess
import sys


def test_runtime_import_does_not_eagerly_import_control_application() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from qb2api.runtime import RuntimeServices"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
