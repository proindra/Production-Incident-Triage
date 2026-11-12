from __future__ import annotations

import httpx
import pytest

from app.infrastructure.llm.resilience import (
    ProviderCircuitOpenError,
    ProviderResilience,
    ProviderUnavailableError,
)


def test_provider_retries_transient_failures_with_exponential_backoff(monkeypatch) -> None:
    delays: list[float] = []
    monkeypatch.setattr(
        "app.infrastructure.llm.resilience.time.sleep", delays.append
    )
    attempts = 0

    def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.TimeoutException("provider timeout")
        return "ok"

    resilience = ProviderResilience(
        max_retries=2,
        initial_backoff_seconds=0.1,
        max_backoff_seconds=0.5,
    )

    assert resilience.invoke(operation) == "ok"
    assert attempts == 3
    assert delays == [0.1, 0.2]


def test_open_circuit_uses_explicit_unavailable_error() -> None:
    resilience = ProviderResilience(
        max_retries=0,
        failure_threshold=1,
        reset_timeout_seconds=30,
    )

    with pytest.raises(ProviderUnavailableError):
        resilience.invoke(
            lambda: (_ for _ in ()).throw(httpx.TimeoutException("timeout"))
        )
    with pytest.raises(ProviderCircuitOpenError):
        resilience.invoke(lambda: "must not run")

    assert resilience.snapshot()["state"] == "open"
    assert resilience.snapshot()["open_count"] == 1
