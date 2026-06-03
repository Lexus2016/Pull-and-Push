"""Web dashboard (spec §12): FastAPI backend + a single-file frontend."""

from .server import create_app

__all__ = ["create_app"]
