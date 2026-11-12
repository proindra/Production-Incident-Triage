"""Redis-backed submission and polling for incident runs."""

from __future__ import annotations

from typing import Any, Protocol

from redis import Redis
from redis.exceptions import RedisError
from rq import Queue
from rq.exceptions import InvalidJobOperationError, NoSuchJobError


class QueueError(RuntimeError):
    """The task queue could not accept or retrieve a run."""


class TaskQueue(Protocol):
    def allow_request(
        self, client_id: str, limit: int = 30, window_seconds: int = 60
    ) -> bool: ...

    def enqueue(
        self,
        request: dict[str, Any],
        correlation_id: str,
        trace_context: dict[str, str] | None = None,
    ) -> str: ...

    def status(self, run_id: str) -> dict[str, Any] | None: ...


class RedisTaskQueue:
    def __init__(self, redis_url: str, queue_name: str = "triage") -> None:
        self.connection = Redis.from_url(redis_url, socket_connect_timeout=3, socket_timeout=3)
        self.queue = Queue(queue_name, connection=self.connection)

    def ping(self) -> bool:
        try:
            return bool(self.connection.ping())
        except RedisError as error:
            raise QueueError("The Redis task queue is unavailable.") from error

    def allow_request(
        self, client_id: str, limit: int = 30, window_seconds: int = 60
    ) -> bool:
        try:
            count = self.connection.eval(
                """
                local count = redis.call('INCR', KEYS[1])
                if count == 1 then
                    redis.call('EXPIRE', KEYS[1], ARGV[1])
                end
                return count
                """,
                1,
                f"triage:rate-limit:{client_id}",
                window_seconds,
            )
        except RedisError as error:
            raise QueueError("Rate limiting is unavailable.") from error
        return int(count) <= limit

    def enqueue(
        self,
        request: dict[str, Any],
        correlation_id: str,
        trace_context: dict[str, str] | None = None,
    ) -> str:
        try:
            job = self.queue.enqueue(
                "app.worker.process_triage",
                request,
                correlation_id,
                trace_context or {},
                job_timeout=120,
                result_ttl=3600,
                failure_ttl=86400,
            )
        except RedisError as error:
            raise QueueError("The triage run could not be queued.") from error
        return job.id

    def status(self, run_id: str) -> dict[str, Any] | None:
        from rq.job import Job

        try:
            job = Job.fetch(run_id, connection=self.connection)
            raw_status = job.get_status(refresh=True)
            status = str(getattr(raw_status, "value", raw_status)).casefold()
        except NoSuchJobError:
            return None
        except (RedisError, InvalidJobOperationError) as error:
            raise QueueError("The triage run status could not be retrieved.") from error

        result = job.result if status == "finished" else None
        return {
            "run_id": run_id,
            "status": status,
            "result": result,
            "error": (
                "The run failed before producing a verified recommendation."
                if status == "failed"
                else None
            ),
        }

    def worker_metrics(self) -> dict[str, str]:
        try:
            values = self.connection.hgetall("triage:worker-metrics")
        except RedisError as error:
            raise QueueError("Worker metrics are unavailable.") from error
        return {
            key.decode("utf-8"): value.decode("utf-8")
            for key, value in values.items()
        }


def record_worker_metrics(
    redis_url: str,
    status: str,
    duration_seconds: float,
    result: dict[str, Any] | None,
) -> None:
    from redis import Redis

    buckets = (0.5, 1, 2.5, 5, 10, 30, 60, 120)
    pipeline = Redis.from_url(
        redis_url, socket_connect_timeout=3, socket_timeout=3
    ).pipeline()
    pipeline.hincrby("triage:worker-metrics", f"runs:{status}", 1)
    pipeline.hincrbyfloat("triage:worker-metrics", "duration_sum", duration_seconds)
    pipeline.hincrby("triage:worker-metrics", "duration_count", 1)
    pipeline.hincrby("triage:worker-metrics", "duration_bucket:+Inf", 1)
    for bucket in buckets:
        if duration_seconds <= bucket:
            pipeline.hincrby(
                "triage:worker-metrics", f"duration_bucket:{bucket}", 1
            )
    if result is not None:
        pipeline.hincrby(
            "triage:worker-metrics", "agent_steps", int(result.get("agent_steps", 0))
        )
        pipeline.hincrby(
            "triage:worker-metrics", "tool_calls", len(result.get("tool_calls", []))
        )
    pipeline.execute()
