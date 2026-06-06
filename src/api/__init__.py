"""HTTP API 层 — 用 FastAPI 暴露 recognize / judge / direct 服务."""

from .server import app, create_app

__all__ = ["app", "create_app"]
