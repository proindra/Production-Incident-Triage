import json

from fastapi.testclient import TestClient

from app.infrastructure.llm.stub import app

client = TestClient(app)


def test_stub_returns_openai_compatible_tool_call(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_STUB_DELAY_SECONDS", "0")

    response = client.post(
        "/openai/v1/chat/completions",
        json={
            "model": "stub-model",
            "messages": [
                {"role": "user", "content": json.dumps({"incident_description": "Disk rising"})}
            ],
            "tools": [{"type": "function", "function": {"name": "search_corpus"}}],
        },
    )

    body = response.json()
    tool_call = body["choices"][0]["message"]["tool_calls"][0]
    assert response.status_code == 200
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert tool_call["function"]["name"] == "lookup_request_fields"
    assert body["usage"]["prompt_tokens"] > 0


def test_stub_summary_reuses_a_passage_citation(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_STUB_DELAY_SECONDS", "0")

    response = client.post(
        "/openai/v1/chat/completions",
        json={
            "model": "stub-model",
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "passages": [
                                {"citation": "postgresql/17/warm-standby.md#Replication Slots"}
                            ]
                        }
                    ),
                }
            ],
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert "[postgresql/17/warm-standby.md#Replication Slots]" in body["choices"][0]["message"]["content"]
