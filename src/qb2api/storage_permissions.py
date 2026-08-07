"""Private filesystem permissions for runtime-owned persistent data."""

from __future__ import annotations

from pathlib import Path


def ensure_private_directory(path: str | Path) -> Path:
    """Create or restrict a runtime-owned directory to its owner."""
    directory = Path(path)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def ensure_private_file(path: str | Path) -> Path:
    """Create or restrict a runtime-owned file to its owner."""
    file_path = Path(path)
    file_path.touch(mode=0o600, exist_ok=True)
    file_path.chmod(0o600)
    return file_path
