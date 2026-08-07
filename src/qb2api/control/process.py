"""Process-group helpers kept separate from Supervisor state logic."""

from __future__ import annotations

import os
import signal
from typing import Any


def process_group(pid: int) -> int:
    try:
        return os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return pid


def send_signal(process: Any, group_id: int, requested: signal.Signals) -> None:
    if group_id and os.name != "nt":
        os.killpg(group_id, requested)
    elif requested == signal.SIGKILL:
        process.kill()
    else:
        process.terminate()
