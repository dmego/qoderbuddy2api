"""Request logger resilience and redacted error coverage."""

import json
import stat
from datetime import datetime

from qb2api.logger import RequestLogger


class TestLogger:
    """Test RequestLogger doesn't crash on various inputs."""

    def test_tool_calls_count_does_not_crash(self, tmp_path):
        logger = RequestLogger(log_dir=str(tmp_path), enabled=True)
        logger.log_request(
            model="deepseek-v3",
            provider="codebuddy",
            stream=False,
            success=True,
            duration=1.0,
            tool_calls_count=2,
        )

    def test_logger_never_raises(self, tmp_path):
        logger = RequestLogger(log_dir=str(tmp_path), enabled=True)
        logger.log_request(
            model="test",
            provider="test",
            stream=False,
            success=True,
            duration=0.1,
            some_random_kwarg=object(),
        )

    def test_error_logged_with_status_code(self, tmp_path):
        logger = RequestLogger(log_dir=str(tmp_path), enabled=True)
        logger.log_request(
            model="deepseek-v3",
            provider="codebuddy",
            stream=False,
            success=False,
            duration=0.5,
            status_code=401,
            error="Unauthorized",
        )

        log_files = list(tmp_path.glob("*.jsonl"))
        assert len(log_files) == 1
        entry = json.loads(log_files[0].read_text().strip())
        assert entry["success"] is False
        assert entry["status_code"] == 401
        assert entry["error"] == "Unauthorized"

    def test_logger_restricts_existing_directory_and_log_file(self, tmp_path):
        log_file = tmp_path / f"requests-{datetime.now():%Y-%m-%d}.jsonl"
        log_file.write_text("{}\n")
        tmp_path.chmod(0o755)
        log_file.chmod(0o644)
        logger = RequestLogger(log_dir=str(tmp_path), enabled=True)

        logger.log_request(
            model="test",
            provider="test",
            stream=False,
            success=True,
            duration=0.1,
        )

        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
        assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
