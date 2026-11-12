"""Stable Uvicorn import path for the HTTP application."""

from app.api.main import app

__all__ = ["app"]
