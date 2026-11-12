from __future__ import annotations

import json
import statistics
import time

from app.domain.schemas import TriageRequest
from app.services.retrieval import RunbookRetriever
from app.services.triage import TriageEngine


def percentile(samples: list[float], quantile: float) -> float:
    ordered = sorted(samples)
    index = min(int((len(ordered) - 1) * quantile), len(ordered) - 1)
    return ordered[index]


def main() -> None:
    query = (
        "Disk usage increasing while table and index sizes are stable; "
        "replication slots restart LSN consumer stalled"
    )
    retriever = RunbookRetriever()
    uncached: list[float] = []
    cached: list[float] = []
    for _ in range(500):
        retriever._retrieve_cached.cache_clear()
        started = time.perf_counter()
        retriever.retrieve(query)
        uncached.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        retriever.retrieve(query)
        cached.append((time.perf_counter() - started) * 1000)

    cold_p95 = percentile(uncached, 0.95)
    warm_p95 = percentile(cached, 0.95)
    engine = TriageEngine()
    request = TriageRequest(
        incident_description=(
            "Disk usage increasing while table and index sizes are stable"
        ),
        context={"disk_growth": "increasing", "table_index_sizes": "stable"},
    )
    cold_runs: list[float] = []
    warm_runs: list[float] = []
    for _ in range(100):
        engine.retriever._retrieve_cached.cache_clear()
        started = time.perf_counter()
        engine.triage(request)
        cold_runs.append((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        engine.triage(request)
        warm_runs.append((time.perf_counter() - started) * 1000)
    cold_run_p95 = percentile(cold_runs, 0.95)
    warm_run_p95 = percentile(warm_runs, 0.95)
    print(
        json.dumps(
            {
                "iterations": len(uncached),
                "uncached_p50_ms": statistics.median(uncached),
                "uncached_p95_ms": cold_p95,
                "cached_p50_ms": statistics.median(cached),
                "cached_p95_ms": warm_p95,
                "retrieval_p95_reduction_percent": (
                    (cold_p95 - warm_p95) / cold_p95 * 100 if cold_p95 else 0
                ),
                "triage_cold_p50_ms": statistics.median(cold_runs),
                "triage_cold_p95_ms": cold_run_p95,
                "triage_warm_p50_ms": statistics.median(warm_runs),
                "triage_warm_p95_ms": warm_run_p95,
                "triage_p95_reduction_percent": (
                    (cold_run_p95 - warm_run_p95) / cold_run_p95 * 100
                    if cold_run_p95
                    else 0
                ),
                "model_cost_usd_per_run": 0,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
