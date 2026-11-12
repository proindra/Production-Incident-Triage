# Incident Runbook Triage Assistant --- Implementation Guide

This guide is the step-by-step implementation plan for GitHub Copilot.
Follow the steps in order. Do not skip ahead without the user's
approval.

## Project goal

Build a safe, advisory-only assistant that helps investigate PostgreSQL
incidents using three existing runbooks:

-   `high_database_latency.md`
-   `too_many_connections.md`
-   `disk_usage_wal_retention.md`

The special scenario is: disk usage is increasing while table and index
sizes remain stable. Investigate whether WAL is being retained,
including by replication slots, but do not assume that is the cause
without evidence.

The assistant may inspect diagnostic data and recommend actions. It must
not automatically drop replication slots, delete WAL files, terminate
production sessions, or change production settings.

## Step 1 --- Create the project structure

Create this structure in the project root:

``` text
incident-runbook-triage/
├── IMPLEMENTATION.md
├── docker-compose.yml
├── prometheus.yml
├── .env.example
├── .gitignore
├── README.md
├── pyproject.toml
├── uv.lock
├── corpus/
│   └── runbooks/
│       ├── high_database_latency.md
│       ├── too_many_connections.md
│       └── disk_usage_wal_retention.md
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── schemas.py
│   ├── policies.py
│   ├── triage.py
│   ├── retrieval.py
│   ├── diagnostics/
│   │   ├── __init__.py
│   │   ├── base.py
│   │   ├── mock.py
│   │   └── postgres_readonly.py
│   └── storage/
│       ├── __init__.py
│       └── incidents.py
├── tests/
│   ├── test_health.py
│   ├── test_policies.py
│   ├── test_latency.py
│   ├── test_connections.py
│   └── test_wal_retention.py
└── docs/
    └── evaluation.md
```

### Files the user will provide

The user will manually place the following five files in the project
root/runbook folder:

1.  `corpus/runbooks/high_database_latency.md`
2.  `corpus/runbooks/too_many_connections.md`
3.  `corpus/runbooks/disk_usage_wal_retention.md`
4.  `docker-compose.yml`
5.  `prometheus.yml`

The three runbooks belong in `corpus/runbooks/`. The Docker Compose and
Prometheus configuration files belong in the project root, alongside
`IMPLEMENTATION.md`.

**Do not create, replace, rewrite, or overwrite these five user-provided
files.** If any is missing, stop and report exactly which file is
missing. Do not make substitute runbooks or configuration files.

### Step 1 Copilot prompt

> Read `IMPLEMENTATION.md` completely. Implement Step 1 only: create the
> project directories and Python placeholder files listed in the guide.
> Do not implement business logic. Do not create, modify, or overwrite
> the three runbooks, `docker-compose.yml`, or `prometheus.yml`; I will
> place those files myself. If a required user-provided file is missing,
> tell me its exact path and stop. After Step 1, show the project tree
> and explain the next step. Do not proceed to Step 2 until I approve.

## Step 2 --- Place and validate the user-provided files

Before starting containers, confirm that these files exist:

-   `docker-compose.yml`
-   `prometheus.yml`
-   `corpus/runbooks/high_database_latency.md`
-   `corpus/runbooks/too_many_connections.md`
-   `corpus/runbooks/disk_usage_wal_retention.md`

The supplied Docker configuration uses project-prefixed container names
(`irt-postgres`, `irt-prometheus`, `irt-grafana`,
`irt-postgres-exporter`), a dedicated network and named volumes. Its
intended host-port mappings are:

  Service                 Host port   Container port
  --------------------- ----------- ----------------
  PostgreSQL                   2026             5432
  Prometheus                   2027             9090
  Grafana                      2028             3000
  PostgreSQL Exporter          2029             9187

Keep the internal container ports in the Compose file unchanged. In
`prometheus.yml`, scrape the in-network targets `irt-prometheus:9090`
and `irt-postgres-exporter:9187`; do not use host ports for
container-to-container scraping.

### Validate and start

Run these commands from the project root:

``` powershell
docker compose config
docker compose up -d
docker compose ps
```

If validation or startup fails, inspect the error and report it. Do not
remove or stop unrelated containers. If a container name, network,
volume, or host port is already in use, report the conflict and ask
before changing/removing existing resources.

Useful checks:

``` powershell
docker compose logs --tail=100
docker compose logs -f irt-postgres
docker compose logs -f irt-prometheus
```

Open:

-   PostgreSQL: host `localhost`, port `2026`
-   Prometheus: `http://localhost:2027`
-   Prometheus targets: `http://localhost:2027/targets`
-   Grafana: `http://localhost:2028`
-   Exporter metrics: `http://localhost:2029/metrics`

Use only local development credentials. Do not expose these development
services publicly. The provided PostgreSQL and Grafana passwords are
examples for local development and should not be used in production.

### Step 2 Copilot prompt

> Implement Step 2 only. Check that the three runbooks and both
> user-provided configuration files exist at the exact paths listed in
> `IMPLEMENTATION.md`. Do not edit those files. Review the Compose and
> Prometheus configurations without overwriting them, then give me the
> exact terminal commands to validate and start the stack. Do not run
> destructive Docker cleanup commands. Explain how to check service
> health and Prometheus targets. Stop and wait for approval before Step
> 3.

## Step 3 --- Set up Python

Use `uv` to manage the project environment and dependencies. The runtime
dependencies and `pytest` development dependency are declared in
`pyproject.toml`. Run `uv sync` to create `.venv`, resolve dependencies,
and generate `uv.lock`; commit the lock file so environments can
reproduce the resolved versions:

```powershell
uv sync
uv run pytest
```

Use `uv add <package>` for runtime dependencies and
`uv add --dev <package>` for development dependencies added later.
After the lock file has been generated and committed, use
`uv sync --locked` to ensure it has not changed unexpectedly.

Do not maintain a separate `requirements.txt`; `pyproject.toml` and
`uv.lock` are the dependency and lock-file sources. Add
retrieval/vector-store dependencies only when implementing retrieval.

Do not place secrets directly in source code; use environment variables
and provide only safe examples in `.env.example`.

## Step 4 --- Define API schemas and health endpoint

Implement:

-   `GET /health` --- returns a small JSON health response.
-   `POST /incidents/triage` --- accepts an incident description and
    optional context, then returns a structured triage result.

Use Pydantic request/response schemas. The response should include:

-   incident category
-   severity and confidence
-   evidence observed
-   checks performed or recommended
-   relevant runbook references
-   recommended next actions
-   escalation guidance
-   explicit uncertainty / missing evidence

Do not claim a diagnostic check ran unless it actually ran. Clearly
distinguish observed facts from hypotheses and recommendations.

## Step 5 --- Centralize severity and safety policy

Implement all severity and safety rules in `app/policies.py`, rather
than scattering them through prompts.

At minimum:

-   Define severity levels and explain the criteria for each.
-   Escalate when evidence suggests serious availability,
    data-integrity, or storage-exhaustion risk.
-   If evidence is insufficient, state what is unknown and request safe
    follow-up checks.
-   Require human approval for risky changes.
-   Never automatically drop replication slots, delete WAL files,
    terminate sessions, or change PostgreSQL settings.
-   Do not reveal credentials or include secrets in logs or incident
    responses.

Add unit tests for policy decisions before adding complex AI behavior.

## Step 6 --- Build diagnostic adapters

Create a common interface in `app/diagnostics/base.py`. Provide:

-   `mock.py` for deterministic sample incidents and tests.
-   `postgres_readonly.py` for optional, read-only PostgreSQL diagnostic
    queries.

Start with mock data so the project works without a live database. When
connecting to PostgreSQL, use a dedicated least-privilege account and
parameterized queries. Never run SQL supplied by the model as arbitrary
executable SQL.

Useful read-only areas to investigate include activity and wait
information, connection counts, database/table/index sizes,
replication-slot state and retained WAL indicators. PostgreSQL
permissions and available columns vary by version and configuration;
verify queries against the version being used. Do not treat one metric
as proof of a root cause.

## Step 7 --- Load and retrieve runbooks

Implement `app/retrieval.py` to:

1.  Read Markdown files only from `corpus/runbooks/`.
2.  Split long runbooks into chunks while preserving headings and source
    filename.
3.  Retrieve relevant chunks for an incident.
4.  Return the runbook filename and heading with each result.
5.  Avoid inventing a runbook or a citation when no relevant text was
    retrieved.

Begin with keyword search if that keeps the first version simple. Add
embeddings/vector search only if needed. The three user-provided
runbooks are the operational source of truth for their procedures; do
not rewrite them.

Official PostgreSQL references for supporting technical context:

-   Monitoring statistics:
    https://www.postgresql.org/docs/17/monitoring-stats.html
-   Connection settings:
    https://www.postgresql.org/docs/17/runtime-config-connection.html
-   Warm standby and replication:
    https://www.postgresql.org/docs/17/warm-standby.html
-   Replication slots:
    https://www.postgresql.org/docs/17/view-pg-replication-slots.html
-   Administrative functions:
    https://www.postgresql.org/docs/17/functions-admin.html

Use documentation that matches the PostgreSQL version under test.

## Step 8 --- Implement the triage workflow

Implement `app/triage.py` to coordinate:

1.  Validate the incoming incident.
2.  Classify the likely incident category.
3.  Select safe diagnostic checks.
4.  Gather evidence from mock or read-only diagnostics.
5.  Retrieve the relevant runbook sections.
6.  Apply severity and safety policies.
7.  Produce a structured, evidence-based response.
8.  Record what was checked, what was not checked, uncertainty, and the
    source runbook.

The model must not invent metrics, claim access it does not have, or
confuse a possible cause with a confirmed cause.

## Step 9 --- Implement the three required scenarios

### A. High database latency

Use the high-latency runbook. Check available latency/activity/wait
evidence, distinguish observation from possible causes, and recommend
the next safe diagnostic steps.

### B. Too many connections

Use the connections runbook. Report current/maximum connection
information only when it is actually available. Recommend safe
investigation and escalation. Do not terminate sessions automatically.

### C. Disk usage rising while table/index sizes are stable

This is the key scenario. Compare the available disk trend with
table/index sizes and inspect relevant WAL/replication-slot evidence
where available. Consider replication-slot WAL retention as a
hypothesis, not an assumed diagnosis.

The result should clearly say:

-   what evidence supports or weakens the WAL-retention hypothesis
-   what evidence is missing
-   which safe checks should be run next
-   which runbook section supports the recommendation
-   when to escalate because disk space is at risk

Never automatically drop a replication slot or delete WAL files.

## Step 10 --- Store incident history

Implement `app/storage/incidents.py` to store incident ID, timestamp,
category, severity, evidence summary, runbook references,
recommendations, and status. Avoid storing credentials or unnecessary
sensitive data. Handle database errors gracefully.

## Step 11 --- Connect monitoring and Grafana

Verify that Prometheus targets are healthy at
`http://localhost:2027/targets`. Add a Prometheus data source in Grafana
at `http://localhost:2028` using the **container-network URL**
`http://irt-prometheus:9090` when configuring it from the Grafana
container. Do not use `localhost:2027` as the Grafana-container
destination.

Build a basic dashboard only after metrics are being scraped. Be
explicit about which metrics are available from the exporter; do not
invent a metric name or claim that a dashboard panel works until
verified.

## Step 12 --- Build a minimal user interface

Provide a simple page or API-driven interface that allows a user to
submit an incident description and view:

-   category and severity
-   evidence and uncertainty
-   relevant runbook references
-   recommended safe checks
-   escalation guidance

Keep the UI small until the API and tests are reliable.

## Step 13 --- Test and evaluate

Add tests for all three scenarios, policy decisions, missing evidence,
retrieval source references, and API validation. Include negative cases
where the cause is unknown and cases where the assistant must refuse to
perform unsafe remediation.

Create at least 20 evaluation cases covering:

-   latency
-   connection exhaustion
-   disk/WAL retention
-   incomplete or conflicting evidence
-   irrelevant or missing runbooks
-   safe escalation
-   refusal of destructive automatic actions

Record expected behavior and results in `docs/evaluation.md`. Prefer
deterministic tests for policy and diagnostic behavior; evaluate
language-model output separately.

## Step 14 --- Security and reliability review

Before the demo:

-   verify that no secrets are committed
-   use least-privilege database access
-   keep diagnostics read-only
-   validate user input
-   use timeouts and handle unavailable services
-   avoid logging credentials
-   verify all runbook citations refer to existing source files
-   document limitations and assumptions
-   ensure no automated destructive remediation exists

## Step 15 --- Demo checklist

Demonstrate:

1.  `GET /health` succeeds.
2.  A high-latency incident returns relevant guidance.
3.  A connection-exhaustion incident returns relevant guidance.
4.  Rising disk usage with stable table/index sizes triggers
    investigation of WAL retention without assuming it is confirmed.
5.  The response cites the relevant runbook filename/section.
6.  Prometheus targets are healthy and Grafana can query Prometheus.
7.  The assistant explains uncertainty and escalation steps.
8.  No destructive action is executed automatically.

## Copilot working rules

-   Implement only the step the user approved.
-   Read this guide before each step.
-   Do not overwrite the three runbooks, `docker-compose.yml`, or
    `prometheus.yml`.
-   Do not perform Docker cleanup or remove volumes unless the user
    explicitly approves.
-   Do not assume a diagnostic query succeeded; report errors honestly.
-   Keep the implementation beginner-friendly and explain commands
    before asking the user to run them.
-   After each step, summarize files changed, tests run, results, and
    the next proposed step.
-   Wait for approval before moving to the next step.

## Definition of done

The project is complete when all three scenarios work end-to-end,
evidence is separated from hypotheses, runbook references are accurate,
the monitoring stack is reachable on the configured ports, evaluation
tests pass, and all remediation remains advisory-only.
