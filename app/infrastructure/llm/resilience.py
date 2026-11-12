"""Bounded provider retries and a per-worker circuit breaker."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

import httpx
from groq import APIError

T = TypeVar("T")


class ProviderUnavailableError(RuntimeError):
    """The provider is unavailable or its circuit breaker is open."""


class ProviderCircuitOpenError(ProviderUnavailableError):
    """A model call was rejected while the provider circuit was open."""


class ProviderResilience:
    def __init__(
        self,
        max_retries: int = 1,
        initial_backoff_seconds: float = 0.25,
        max_backoff_seconds: float = 2.0,
        failure_threshold: int = 3,
        reset_timeout_seconds: float = 30.0,
    ) -> None:
        if max_retries < 0 or failure_threshold < 1:
            raise ValueError("Retry count and failure threshold are out of range.")
        if initial_backoff_seconds < 0 or max_backoff_seconds < initial_backoff_seconds:
            raise ValueError("Provider retry backoff settings are invalid.")
        if reset_timeout_seconds <= 0:
            raise ValueError("Circuit-breaker reset timeout must be positive.")
        self.max_retries = max_retries
        self.initial_backoff_seconds = initial_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._open_count = 0

    def invoke(self, operation: Callable[[], T]) -> T:
        self._ensure_closed_or_recovered()
        for attempt in range(self.max_retries + 1):
            try:
                result = operation()
            except (APIError, httpx.TransportError, TimeoutError) as error:
                self._record_failure()
                if (
                    attempt >= self.max_retries
                    or not self._is_retryable(error)
                    or self._is_open()
                ):
                    raise ProviderUnavailableError(
                        "The model provider request failed."
                    ) from error
                delay = min(
                    self.initial_backoff_seconds * (2**attempt),
                    self.max_backoff_seconds,
                )
                if delay:
                    time.sleep(delay)
            else:
                self._record_success()
                return result
        raise ProviderUnavailableError("The model provider request failed.")

    def snapshot(self) -> dict[str, int | float | str]:
        with self._lock:
            now = time.monotonic()
            is_open = self._open_until > now
            return {
                "state": "open" if is_open else "closed",
                "consecutive_failures": self._consecutive_failures,
                "open_count": self._open_count,
                "seconds_until_reset": max(0.0, self._open_until - now),
            }

    def _ensure_closed_or_recovered(self) -> None:
        with self._lock:
            now = time.monotonic()
            if self._open_until > now:
                raise ProviderCircuitOpenError(
                    "The model provider circuit is open; deterministic fallback is required."
                )
            if self._open_until:
                self._open_until = 0.0
                self._consecutive_failures = 0

    def _record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._open_until = time.monotonic() + self.reset_timeout_seconds
                self._open_count += 1

    def _record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._open_until = 0.0

    def _is_open(self) -> bool:
        with self._lock:
            return self._open_until > time.monotonic()

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        if isinstance(error, (httpx.TransportError, TimeoutError)):
            return True
        status_code = getattr(error, "status_code", None)
        return status_code in (408, 429) or (
            isinstance(status_code, int) and status_code >= 500
        )
