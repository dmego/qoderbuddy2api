"""Request logging."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("qb2api")


class RequestLogger:
    """Logs API requests to file and console."""

    def __init__(self, log_dir: str = "./logs", enabled: bool = True):
        self.log_dir = Path(log_dir)
        self.enabled = enabled
        if enabled:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    def log_request(
        self,
        model: str,
        provider: str,
        stream: bool,
        success: bool,
        duration: float,
        status_code: int = 200,
        error: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Log a chat completion request. Never raises — logging failures don't affect main request."""
        try:
            self._do_log(model, provider, stream, success, duration, status_code, error, **kwargs)
        except Exception as e:
            logger.error(f"Failed to write request log: {e}")

    def _do_log(
        self,
        model: str,
        provider: str,
        stream: bool,
        success: bool,
        duration: float,
        status_code: int,
        error: str | None,
        **kwargs: Any,
    ) -> None:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "model": model,
            "provider": provider,
            "stream": stream,
            "success": success,
            "duration_seconds": round(duration, 3),
            "status_code": status_code,
        }

        # Add optional fields (reasoning_effort, context_window, etc.)
        for key in ("reasoning_effort", "context_window", "max_tokens", "tool_calls_count"):
            if key in kwargs and kwargs[key] is not None:
                entry[key] = kwargs[key]

        if error:
            entry["error"] = error

        # Console log
        status = "✅" if success else "❌"
        effort = f" effort={kwargs.get('reasoning_effort')}" if kwargs.get("reasoning_effort") else ""
        tc_count = kwargs.get("tool_calls_count")
        tools = f" tools={tc_count}" if tc_count else ""
        logger.info(
            f"{status} {provider}/{model} {'stream' if stream else 'sync'} "
            f"{duration:.2f}s{effort}{tools}"
            + (f" error={error}" if error else "")
        )

        # File log
        if self.enabled:
            today = datetime.now().strftime("%Y-%m-%d")
            log_file = self.log_dir / f"requests-{today}.jsonl"
            with open(log_file, "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
