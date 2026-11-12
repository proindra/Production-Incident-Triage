"""Local OpenAI-compatible provider stub for deterministic load and failure tests."""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from fastapi.responses import Response

app = FastAPI(title="Incident Triage Model Stub")
REQUESTS = Counter(
    "incident_triage_model_stub_requests_total",
    "Requests served by the deterministic model stub.",
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/openai/v1/chat/completions")
async def chat_completions(request: Request) -> dict[str, Any]:
    REQUESTS.inc()
    body = await request.json()
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="messages must be a list.")
    try:
        delay = float(os.getenv("MODEL_STUB_DELAY_SECONDS", "1"))
    except ValueError as error:
        raise HTTPException(
            status_code=500, detail="MODEL_STUB_DELAY_SECONDS is invalid."
        ) from error
    if not 0 <= delay <= 30:
        raise HTTPException(
            status_code=500, detail="MODEL_STUB_DELAY_SECONDS must be between 0 and 30."
        )
    if delay:
        await asyncio.sleep(delay)

    tools = body.get("tools")
    tool_messages = [
        message for message in messages if message.get("role") == "tool"
    ]
    if isinstance(tools, list) and tools:
        message, finish_reason = _tool_selection(messages, tool_messages)
    else:
        message, finish_reason = _summary(messages), "stop"

    prompt_tokens = sum(
        len(str(message.get("content", "")).split()) for message in messages
    )
    completion_tokens = len(str(message.get("content", "")).split())
    return {
        "id": f"stub-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(body.get("model", "stub-model")),
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _tool_selection(
    messages: list[dict[str, Any]],
    tool_messages: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    count = len(tool_messages)
    request_payload = _last_user_json(messages)
    description = str(request_payload.get("incident_description", ""))
    context = request_payload.get("context") or {}
    disk_case = (
        "disk" in description.casefold()
        and "table" in description.casefold()
        and "stable" in description.casefold()
    )
    if count == 0:
        return _tool_call(
            "lookup_request_fields",
            {"request_json": json.dumps(request_payload, sort_keys=True)},
        ), "tool_calls"
    if count == 1:
        query = description + " " + " ".join(
            f"{key} {value}" for key, value in context.items()
        )
        return _tool_call(
            "search_corpus", {"query": query[:1000], "limit": 4}
        ), "tool_calls"
    if disk_case and count == 2:
        return _tool_call(
            "lookup_clause_by_id",
            {
                "source_id": (
                    "postgresql/17/warm-standby.md#26.2.6. Replication Slots"
                )
            },
        ), "tool_calls"
    if disk_case and count == 3:
        return _tool_call(
            "search_corpus",
            {
                "query": (
                    "replication slots retained WAL restart_lsn consumer progress"
                ),
                "limit": 4,
            },
        ), "tool_calls"
    return {"role": "assistant", "content": "Evidence retrieval is complete."}, "stop"


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": f"stub-call-{uuid.uuid4()}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, sort_keys=True),
                },
            }
        ],
    }


def _last_user_json(messages: list[dict[str, Any]]) -> dict[str, Any]:
    content = next(
        (
            message.get("content")
            for message in reversed(messages)
            if message.get("role") == "user"
        ),
        None,
    )
    if not isinstance(content, str):
        return {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _summary(messages: list[dict[str, Any]]) -> dict[str, str | None]:
    payload = _last_user_json(messages)
    passages = payload.get("passages")
    system_prompt = next(
        (
            str(message.get("content", ""))
            for message in messages
            if message.get("role") == "system"
        ),
        "",
    )
    citation = None
    passage_text = ""
    if isinstance(passages, list) and passages:
        first = passages[0]
        if isinstance(first, dict) and isinstance(first.get("citation"), str):
            citation = first["citation"]
            passage_text = str(first.get("text", ""))
    if "grounded-summary-v1" in system_prompt:
        content = (
            "The cause is confirmed and requires immediate remediation."
            if citation is None
            else f"The cause is confirmed and requires immediate remediation [{citation}]."
        )
    else:
        quote = " ".join(passage_text.split()[:16])
        content = quote if citation is None else f"{quote} [{citation}]"
    return {"role": "assistant", "content": content}
