from enum import Enum

from app.infrastructure.queue.queueing import RedisTaskQueue


class JobStatus(Enum):
    FINISHED = "finished"


class FinishedJob:
    result = {"category": "high_database_latency"}

    def get_status(self, refresh: bool = False) -> JobStatus:
        return JobStatus.FINISHED


def test_rq_enum_status_is_normalized_for_polling(monkeypatch) -> None:
    monkeypatch.setattr(
        "rq.job.Job.fetch",
        lambda run_id, connection: FinishedJob(),
    )
    queue = RedisTaskQueue("redis://localhost:6379/0")

    result = queue.status("test-run")

    assert result == {
        "run_id": "test-run",
        "status": "finished",
        "result": {"category": "high_database_latency"},
        "error": None,
    }
