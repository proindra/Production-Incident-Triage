"""Stable RQ import path for the triage task."""

from app.infrastructure.queue.worker import process_triage

__all__ = ["process_triage"]
