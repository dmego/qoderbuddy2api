"""Persistent Control Plane services."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import create_control_app


def __getattr__(name: str):
    if name == "create_control_app":
        from .app import create_control_app

        return create_control_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["create_control_app"]
