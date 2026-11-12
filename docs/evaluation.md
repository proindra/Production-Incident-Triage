# Evaluation and performance

## Service-level objective and load test

**Declared SLO: p95 submit-to-final-result latency at or below 30 seconds.**
The brief describes an on-call engineer responding under fatigue and time
pressure; a 30-second budget allows for multi-step retrieval and verification
while still returning actionable guidance promptly. The measured interval
starts when Locust submits `POST /incidents/triage` and ends only when polling
returns the completed result.

The worker's `TRIAGE_STUB_DELAY_SECONDS` setting simulates fixed provider
latency without provider rate limits. Keep the same request mix and delay when
comparing worker counts. Increase `RATE_LIMIT_PER_MINUTE` only for the local
load run so the test measures service capacity rather than the development
budget.

Example PowerShell run:

```powershell
$env:RATE_LIMIT_PER_MINUTE = "600"
$env:TRIAGE_STUB_DELAY_SECONDS = "1"
docker compose up --build -d --force-recreate --scale agent-worker=1 api agent-worker
uv run locust -f scripts/locustfile.py --headless -H http://localhost:8000 -u 10 -r 2 -t 60s --csv=results/one-worker
docker compose up --build -d --force-recreate --scale agent-worker=3 api agent-worker
uv run locust -f scripts/locustfile.py --headless -H http://localhost:8000 -u 10 -r 2 -t 60s --csv=results/three-workers
```

Record complete-run RPS, p50, p95, failure rate, and the concurrency at which
p95 first exceeds 30 seconds. Compare queue wait/worker execution with the API
submission histogram. The API's `/metrics` histogram measures HTTP request
time, while Locust's `complete triage run` event is the end-to-end measurement.

### Measured results

The runs below used a fixed 1-second worker delay, a 1,000-submission/minute
local test limit, and 60 seconds per Locust run. RPS, p50, and p95 are from
Locust's `complete triage run` metric, not its individual polling requests.

| Workers | Users | Complete runs | RPS | p50 | p95 | Failures | SLO |
|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | 10 | 25 | 0.423 | 23.0 s | 23.0 s | 0 | Met |
| 3 | 10 | 72 | 1.212 | 7.9 s | 8.9 s | 0 | Met |
| 1 | 15 | 24 | 0.411 | 28.0 s | 36.0 s | 0 | Failed |

At 10 concurrent users, three workers increased throughput by about **2.9x**
and reduced p95 by about **61%**. The first failing tested concurrency was 15
users with one worker; 10 users passed. The bottleneck is serialized worker
execution and queue wait, not the API submission path. At 15 users the target
is exceeded even though no runs failed.

These Locust results were collected with RAG disabled, so they measure the
deterministic workflow and configured worker delay, not Groq latency or model
provider rate limits. Run a separate provider-stub comparison before using
these figures to claim an SLO for Groq-enabled runs.

The runbook retrieval/triage cache benchmark is available with
`uv run --locked python -m scripts.benchmark_retrieval` (500 retrieval pairs
and 100 triage pairs, no queue or simulated delay). On this workstation the
cold/warm retrieval p95 was **0.795/0.0068 ms**; full deterministic triage
p95 was **9.48/6.77 ms**, a **28.6%** reduction in the triage computation
component. This is not a claim that the cache reduces queue-dominated
submit-to-result p95 by that amount. No LLM is called, so model spend is
**$0 per 100 runs** in this implementation.

## Deterministic evaluation cases

The deterministic suite in `tests/test_evaluation.py` covers these 20 cases.
The expected outcomes are asserted in every case. The tests use no live
PostgreSQL connection.

Latest validation: see the current test and container checks recorded at task
completion. These unit/integration cases do not substitute for the Locust
performance report.

| # | Case | Expected behavior |
|---:|---|---|
| 1 | Latency alert without metrics | Classify latency, medium severity, state that live checks did not run. |
| 2 | Latency at least 3x supplied threshold | Classify latency and assign high severity. |
| 3 | Latency with unavailable checks | Preserve empty performed-check list and recommend read-only checks. |
| 4 | Latency with failing customer requests | Escalate as high without asserting a root cause. |
| 5 | 50/100 connections | Report supplied counts; do not claim a live query ran. |
| 6 | 96/100 connections | Assign high severity and cite the connection runbook. |
| 7 | Connection alert with no counts | State that current/maximum counts are missing. |
| 8 | Application cannot connect | Escalate promptly; do not terminate sessions. |
| 9 | Disk at 88%, growing, table sizes stable | Treat WAL retention as possible; do not confirm it. |
| 10 | Disk at 90% | Assign high severity and recommend headroom checks. |
| 11 | Disk at 95% | Assign critical severity and immediate escalation. |
| 12 | Disk free space at 4% | Assign critical severity even without a confirmed cause. |
| 13 | Disk growth, no slot evidence | Request slot, WAL, free-space, and consumer evidence. |
| 14 | Inactive slot only | Do not treat inactivity as proof of WAL retention. |
| 15 | Conflicting table-size trend | Preserve uncertainty; do not claim stable tables. |
| 16 | Disk growth could be logs/backups | Recommend checking other consumers of the volume. |
| 17 | Unsupported incident | Return unknown/low and request more incident detail. |
| 18 | Irrelevant weather query | Return no runbook citation. |
| 19 | Request to delete WAL | Refuse destructive remediation and keep advice non-destructive. |
| 20 | Request to terminate sessions | Refuse termination and require human approval for changes. |

Focused tests also cover latency/activity evidence, connection counts, the
WAL-retention hypothesis and its missing evidence, policy boundaries, typed
alert validation, request quarantine before queue submission, citation
verification and fallback escalation, asynchronous status polling, feedback
persistence/aggregation, rate limiting, source headings, and the health/API
routes.

## Manual demo checks

1. `GET /health` returns `{"status":"ok"}`.
2. Submit a request and poll the returned `status_url`; confirm the accepted
   response contains a run ID and the final result cites a source filename and
   heading.
3. For rising disk usage with stable table/index sizes, confirm WAL retention is
   labeled a hypothesis and missing consumer/free-space evidence is identified.
4. Confirm the adapter reports no performed checks when it is using the default
   mock and no configured fixtures.
5. Check Prometheus `/api/v1/targets` or `/targets`; the API, Prometheus, and
   database exporter jobs should be healthy. Confirm exporter metric names at
   `/metrics`; the Grafana dashboard also includes API request throughput and
   p95 latency.
6. Submit a request for destructive remediation and confirm the response
   refuses it; no automated remediation is implemented.
7. Submit satisfactory and unsatisfactory feedback and check the aggregated
   `/incidents/feedback/summary` response.
