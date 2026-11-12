# Evaluation cases

The deterministic suite in `tests/test_evaluation.py` covers these 20 cases.
The expected outcomes are asserted in every case. The tests use no live
PostgreSQL connection.

Latest validation: `uv sync --locked`, Python compilation, Docker Compose
configuration, and all 60 pytest tests passed. The test runner reports one
Starlette/httpx deprecation warning from the existing dependency versions.

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
WAL-retention hypothesis and its missing evidence, policy boundaries, request
validation, runbook source headings, history persistence without raw incident
text, and the health and triage API routes.

## Manual demo checks

1. `GET /health` returns `{"status":"ok"}`.
2. Submit each of the three runbook scenarios using `POST /incidents/triage` or
   the UI; confirm the correct source filename and heading are returned.
3. For rising disk usage with stable table/index sizes, confirm WAL retention is
   labeled a hypothesis and missing consumer/free-space evidence is identified.
4. Confirm the adapter reports no performed checks when it is using the default
   mock and no configured fixtures.
5. Check Prometheus `/api/v1/targets` or `/targets`; both configured jobs should
   be healthy. Confirm exporter metric names at `/metrics`; the checked-in
   Grafana dashboard queries `pg_up`, `pg_stat_database_numbackends`, and
   `pg_database_size_bytes`.
6. Submit a request for destructive remediation and confirm the response
   refuses it; no automated remediation is implemented.
