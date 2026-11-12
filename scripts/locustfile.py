"""Measure complete triage runs, including queue wait and worker execution."""

from __future__ import annotations

import time

from locust import HttpUser, task


class IncidentTriageUser(HttpUser):
    wait_time = lambda self: 0

    @task
    def submit_and_poll(self) -> None:
        started = time.perf_counter()
        try:
            submission = self.client.post(
                "/incidents/triage",
                json={
                    "incident_description": (
                        "Disk usage increasing while table and index sizes are stable"
                    ),
                    "context": {
                        "disk_used_percent": 88,
                        "disk_growth": "increasing",
                        "table_index_sizes": "stable",
                    },
                },
                name="/incidents/triage",
            )
            if submission.status_code != 202:
                raise RuntimeError(f"Submission returned HTTP {submission.status_code}")
            status_url = submission.json()["status_url"]
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                response = self.client.get(status_url, name="/incidents/runs/{run_id}")
                if response.status_code != 200:
                    raise RuntimeError(f"Status returned HTTP {response.status_code}")
                status = response.json()
                if status["status"] == "finished":
                    if not status["result"]["verification_passed"]:
                        raise RuntimeError("Run did not pass citation verification")
                    self._record_total(started, None)
                    return
                if status["status"] == "failed":
                    raise RuntimeError(status.get("error") or "Worker failed")
                time.sleep(0.1)
            raise TimeoutError("Run did not finish within the 120 second task limit")
        except Exception as error:
            self._record_total(started, error)

    def _record_total(self, started: float, error: Exception | None) -> None:
        self.environment.events.request.fire(
            request_type="RUN",
            name="complete triage run",
            response_time=(time.perf_counter() - started) * 1000,
            response_length=0,
            response=None,
            context={},
            exception=error,
        )
