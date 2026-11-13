# Architecture and run boundaries

## Code layers

- `app/api` handles HTTP requests and the browser UI.
- `app/domain` contains shared triage models, policies, and ports, without
  infrastructure dependencies.
- `app/services` coordinates triage, retrieval, and agent workflows through
  those domain contracts.
- `app/infrastructure` contains Redis/RQ, SQLite, synthetic diagnostics, and
  optional RAG adapters.
- `app/core` provides shared configuration and observability setup.

The root `app/main.py` and `app/worker.py` modules preserve the existing runtime
entry points; their implementations live in the API and infrastructure layers.

## Request path

```text
Browser / API client submits a synthetic or replayed alert
    -> FastAPI validation, quarantine, correlation ID, per-client rate limit
    -> Redis/RQ queue (HTTP 202 with run ID)
    -> agent-worker replica
        -> LangGraph Triage Agent
            -> request-field lookup
            -> model-selected tools when enabled, otherwise deterministic tool flow
            -> keyword search (default) or semantic FAISS search (RAG mode)
            -> clause lookup when the disk/WAL scenario calls for it
            -> deterministic synthetic diagnostic fixtures only
        -> optional Groq summary over retrieved passages
        -> LangGraph Verification Agent
            -> check every citation against retrieved source text
            -> one alternate search if verification fails
            -> escalate rather than return an unverified recommendation
        -> SQLite incident history
    <- status endpoint returns the finished result
```

The triage agent owns incident interpretation, retrieval strategy, and a draft
recommendation. It skips corpus search for unsupported incidents and searches
the corpus only after classifying the request into a supported category. It
hands the draft and the exact retrieved references to a
separate verification node. The verifier checks that each citation resolves to
the original runbook text and that a supported category has its corresponding
runbook. It can request one alternative search; it cannot approve unsupported
text. The graph is bounded by an eight-tool-call limit, one retry, and the RQ
120-second job timeout.

Severity, routing, and recommended actions remain deterministic. With
`RAG_ENABLED=false` and `AGENT_TOOL_SELECTION_ENABLED=false`, the service makes
no model calls. In RAG mode, the worker uses local FastEmbed embeddings with a
persistent FAISS index and calls Groq to generate a short, cited summary.
Model-selected tool use is separately gated by
`AGENT_TOOL_SELECTION_ENABLED`; it is bounded and cannot change the deterministic
policy or perform remediation. Local lexical input/output safety checks are
enabled, but they are not a general toxicity or prompt-injection classifier.
The summary records a prompt version and provider-reported token counts; cost
estimates are emitted only when both token prices are configured.

## Tools

| Tool | Purpose |
|---|---|
| `lookup_request_fields` | Return the request description and validated context fields. |
| `search_corpus` | Search runbook chunks by keyword by default or by FAISS similarity in RAG mode; return source filename, heading, and excerpt. |
| `lookup_clause_by_id` | Retrieve a runbook clause by `filename#heading` when a follow-up is warranted. |

Tool name, sanitized query, result count, and latency are returned with the run.
OpenTelemetry spans cover the API request, worker run, agent nodes, and tool
calls. The correlation ID and W3C trace context travel through the queued job.

## Failure behavior and service boundaries

- Invalid input returns `422`, field-level errors, and a quarantine ID before
  queue submission.
- Redis unavailable or rate-limited submission returns `503` or `429`; an
  unaccepted request does not start a worker.
- A worker failure or timeout remains a failed run and is not converted into a
  successful result.
- The worker never connects to a database or monitoring target. Evidence comes
  only from the synthetic/replayed alert payload; no live checks are run.
- Missing/invalid citations trigger one alternate retrieval; if verification
  still fails, the result is changed to `unknown` and routed to a human.
- The API, Redis, and worker are separate Compose services so the API remains
  responsive during long runs and workers can scale independently. Prometheus,
  Grafana, and Jaeger provide local service observability.

The optional FAISS ingestion command is
`python -m app.infrastructure.rag.document_ingestion`. It keeps unchanged
embeddings, replaces changed chunks, removes deleted chunks, and re-embeds the
corpus when the configured embedding model changes. The default chunker
preserves Markdown headings and uses approximately 1,200 characters per chunk.
Download PostgreSQL 17 documentation before indexing with:

```powershell
python -m app.infrastructure.rag.document_ingestion.fetch_docs --version 17 --destination data/postgresql
```

The API exports request counters and latency histograms at `/metrics`.
`RATE_LIMIT_PER_MINUTE` controls per-client submission limits. The default
retrieval cache is a bounded in-process LRU; it caches only runbook retrieval,
not diagnostics or complete incident runs.

## Evaluation/load testing

The SLO and load procedure are declared in [evaluation.md](./evaluation.md).
`scripts/locustfile.py` measures submit-to-final-result time, including queue wait and
worker execution, rather than only the HTTP submission latency. Set
`TRIAGE_STUB_DELAY_SECONDS` to simulate fixed provider latency for a load run;
the previously reported load results used deterministic mode and do not measure
Groq provider latency. `scripts/evaluate_rag.py` compares the two versioned
summary prompts and reports retrieval relevance, lexical grounding, and
citation validity; its lexical scores are proxies rather than semantic or
human-judged results.
