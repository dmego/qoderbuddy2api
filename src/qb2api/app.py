"""Compatibility entrypoint for the persistent Control Plane."""

from qb2api.control.app import create_control_app

create_app = create_control_app
app = create_control_app()

__all__ = ["app", "create_app"]
