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
        *,
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
            self._do_log(
                model=model,
                provider=provider,
                stream=stream,
                success=success,
                duration=duration,
                status_code=status_code,
                error=error,
                **kwargs,
            )
        except Exception as e:
            logger.error(f"Failed to write request log: {e}")

    def _do_log(
        self,
        *,
        model: str,
        provider: str,
        stream: bool,
        success: bool,
        duration: float,
        status_code: int,
        error: str | None,
        **kwargs: Any,
    ) -> None:
        entry = self._entry(
            model=model,
            provider=provider,
            stream=stream,
            success=success,
            duration=duration,
            status_code=status_code,
            error=error,
            extras=kwargs,
        )
        logger.info(
            self._console_message(
                model=model,
                provider=provider,
                stream=stream,
                success=success,
                duration=duration,
                error=error,
                extras=kwargs,
            )
        )
        if self.enabled:
            self._write_entry(entry)

    @staticmethod
    def _entry(
        *,
        model: str,
        provider: str,
        stream: bool,
        success: bool,
        duration: float,
        status_code: int,
        error: str | None,
        extras: dict[str, Any],
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(),
            "model": model,
            "provider": provider,
            "stream": stream,
            "success": success,
            "duration_seconds": round(duration, 3),
            "status_code": status_code,
        }
        entry.update(_log_extras(extras))
        if error:
            entry["error"] = error
        return entry

    @staticmethod
    def _console_message(
        *,
        model: str,
        provider: str,
        stream: bool,
        success: bool,
        duration: float,
        error: str | None,
        extras: dict[str, Any],
    ) -> str:
        status = "✅" if success else "❌"
        effort = f" effort={extras.get('reasoning_effort')}" if extras.get("reasoning_effort") else ""
        tc_count = extras.get("tool_calls_count")
        tools = f" tools={tc_count}" if tc_count else ""
        return (
            f"{status} {provider}/{model} {'stream' if stream else 'sync'} "
            f"{duration:.2f}s{effort}{tools}"
            + (f" error={error}" if error else "")
        )

    def _write_entry(self, entry: dict[str, Any]) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = self.log_dir / f"requests-{today}.jsonl"
        with open(log_file, "a") as file:
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _log_extras(values: dict[str, Any]) -> dict[str, Any]:
    keys = ("reasoning_effort", "context_window", "max_tokens", "tool_calls_count")
    return {key: values[key] for key in keys if values.get(key) is not None}
