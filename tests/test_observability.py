import json
import logging

from app.core.observability import (
    JsonLogFormatter,
    correlation_attributes,
    correlation_context,
)


def test_json_logs_and_agent_span_attributes_use_active_correlation_id() -> None:
    record = logging.LogRecord(
        "test.observability",
        logging.INFO,
        __file__,
        1,
        "triage step completed",
        (),
        None,
    )
    formatter = JsonLogFormatter()

    with correlation_context("request-123"):
        event = json.loads(formatter.format(record))
        attributes = correlation_attributes()

    assert event["correlation_id"] == "request-123"
    assert attributes == {"correlation_id": "request-123"}
    assert "correlation_id" not in json.loads(formatter.format(record))
