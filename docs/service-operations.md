# Service failure runbook and development incident log

## Redis queue unavailable

**Symptoms:** `POST /incidents/triage` returns `503`, or API readiness fails.

**Confirm:** Check `docker compose ps redis api` and Redis health with
`docker compose exec redis redis-cli ping`. Do not submit a request repeatedly
while Redis is unhealthy; it was not accepted by the queue.

**Recovery:** Check Redis container logs with `docker compose logs redis`.
Restore the Redis service and confirm it is healthy, then retry the request
once. Do not remove the Redis volume as a recovery step.

## Worker stopped or run failed

**Symptoms:** A run remains queued, or polling returns `failed`.

**Confirm:** Check `docker compose ps agent-worker` and
`docker compose logs --tail=100 agent-worker`. A failed run is not presented as
a completed recommendation.

**Recovery:** Correct the reported worker/configuration issue, start or recreate
the worker service, and verify a new run reaches `finished` with
`verification_passed: true`. Do not replay a failed job automatically.

## Development incident log

| Date | Symptom | Cause | Recovery and prevention |
|---|---|---|---|
| 2026-11-12 | Containerized triage completed, but the polling client did not recognize the terminal status and returned no result. | RQ 3 returns a `JobStatus` enum whose default string form is `JobStatus.FINISHED`, not `finished`. | Normalize the enum through its `.value` before status comparison. Added a regression test and verified a real queued disk/WAL run returns `finished`, its verified result, and all selected tool calls. |
