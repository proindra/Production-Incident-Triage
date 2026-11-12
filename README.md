<h1 align="center">PostgreSQL Incident Triage Assistant</h1>

<p align="center">
  <strong>Investigate database incidents. Get safe next steps with runbook sources.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/API-FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Language-Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Database-PostgreSQL-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/Metrics-Prometheus-E6522C?style=for-the-badge&logo=prometheus&logoColor=white" alt="Prometheus">
  <img src="https://img.shields.io/badge/Dashboards-Grafana-F46800?style=for-the-badge&logo=grafana&logoColor=white" alt="Grafana">
  <img src="https://img.shields.io/badge/Safety-Advisory--Only-2EA44F?style=for-the-badge" alt="Advisory only">
</p>

<p align="center">
  Triage high database latency, connection exhaustion, and increasing disk usage with evidence-based recommendations and citations from the project runbooks. The FastAPI service uses deterministic mock diagnostics by default, with optional read-only PostgreSQL checks. It distinguishes observed evidence from hypotheses and never performs production remediation.
</p>

---

## Run locally

Requirements: Python 3.11+, `uv`, and (only for the monitoring stack) Docker
Compose.

```powershell
uv sync --locked
uv run pytest
uv run uvicorn app.main:app --reload
```

Open the UI at <http://127.0.0.1:8000/> or the API docs at
<http://127.0.0.1:8000/docs>. `GET /health` checks that the API process is
responsive. `POST /incidents/triage` accepts a description and optional
structured context. For example:

```json
{
  "incident_description": "Disk usage increasing while table/index sizes remain stable",
  "context": {
    "disk_used_percent": 88,
    "disk_growth": "increasing",
    "table_index_sizes": "stable"
  }
}
```

The response separates user-supplied facts, checks actually performed,
hypotheses, safe recommended checks, uncertainties, runbook citations, severity,
and escalation guidance. Context is capped at 100 fields and 10,000 serialized
characters. A local SQLite history (`incidents.db`) stores only triage metadata
and a non-sensitive summary, not the incident text or raw context.

## Optional live PostgreSQL diagnostics

Without `DATABASE_URL`, the service uses an empty deterministic mock adapter:
it does not fabricate metrics or claim live checks ran. To enable PostgreSQL
diagnostics, provision a dedicated least-privilege role instead of using the
Compose bootstrap superuser:

```powershell
Get-Content .\scripts\create_readonly_role.sql -Raw | docker compose exec -T irt-postgres psql -U incident_user -d incidentdb
docker compose exec irt-postgres psql -U incident_user -d incidentdb
```

At the interactive `psql` prompt, set a local password with
`\password incident_readonly`, then exit with `\q`. Set the environment variable
before starting the API (URL-encode any reserved characters in the password):

```powershell
$env:DATABASE_URL = "postgresql://incident_readonly:<local-password>@localhost:2026/incidentdb"
uv run uvicorn app.main:app --reload
```

The adapter only runs static `SELECT` statements inside a read-only transaction,
uses a three-second connection and statement timeout, and reports unavailable
checks rather than treating them as successful. PostgreSQL permissions and
available monitoring columns can vary; host disk/free-space trends still need
to come from monitoring or the managed database service. Do not put real
credentials in source control, incident descriptions, or logs. `.env.example`
contains only local-development examples.

## Optional monitoring stack

The supplied Compose and Prometheus files are the local development setup.
They expose PostgreSQL at `localhost:2026`, Prometheus at `localhost:2027`,
Grafana at `localhost:2028`, and the PostgreSQL exporter at `localhost:2029`.
Keep these services local; the example credentials are not production
credentials.

### Local container access and credentials

| Service/container | Local address | Username | Password | Notes |
|---|---|---|---|---|
| PostgreSQL (`irt-postgres`) | `localhost:2026` | `incident_user` | `local_dev_password` | Database: `incidentdb`. These are the Compose bootstrap credentials. |
| Grafana (`irt-grafana`) | <http://localhost:2028> | `admin` | `local_grafana_password` | Grafana UI login. |
| Prometheus (`irt-prometheus`) | <http://localhost:2027> | None | None | No authentication is configured in the supplied local Compose setup. |
| PostgreSQL Exporter (`irt-postgres-exporter`) | <http://localhost:2029/metrics> | None for the metrics endpoint | None | Scrapes PostgreSQL using `incident_user` and the PostgreSQL password above over the Docker network. |
| Triage API | <http://localhost:8000> | None | None | No authentication is configured; run locally only. |

For the API's optional live diagnostics, use the separate `incident_readonly`
role and a password you set yourself as described above; it does not have a
default password in the project. Prometheus, the exporter metrics endpoint,
and the API are unauthenticated in this development setup. Do not expose them
to an untrusted network, and change/reconfigure all credentials before any
shared or production deployment.

```powershell
docker compose config
docker compose up -d
docker compose ps
```

Check <http://localhost:2027/targets> for both scrape targets. In Grafana, add a
Prometheus data source with URL `http://irt-prometheus:9090` (Grafana must use
the container-network URL, not `localhost:2027`). The exporter exposes metrics
such as `pg_database_size_bytes` and `pg_stat_database_numbackends`; verify
available metrics at <http://localhost:2029/metrics> before building panels.
The checked-in dashboard at
[`grafana/incident-triage-dashboard.json`](grafana/incident-triage-dashboard.json)
uses verified `pg_up`, `pg_stat_database_numbackends`, and
`pg_database_size_bytes` queries. Import it from Grafana's dashboard UI and
select the `Incident Triage Prometheus` data source. The currently running
local Grafana stack has that data source and the three-panel dashboard
configured and query-verified.

The supplied Compose file uses its local bootstrap database credentials for
the exporter. This is suitable only for the isolated development demo; do not
expose this stack or reuse those credentials in a shared/production deployment.
The application adapter can use the separate `incident_readonly` role above.

## Safety and limitations

- Triage is advisory; no remediation endpoint or automatic database change
  exists. Dropping slots, deleting WAL, terminating sessions, and changing
  server settings are explicitly refused.
- User context is treated as reported evidence, not as a diagnostic result.
- Severity thresholds are project defaults in `app/policies.py`; teams should
  replace them with their approved incident policy.
- Keyword retrieval returns citations only when matching text exists in the
  supplied runbooks. The runbooks are read-only source material.
- The default SQLite history is local and is not intended as shared or
  production storage.
- The demo UI is intentionally small and should be protected behind suitable
  authentication and network controls before any shared deployment.

## Implementation progress

- [x] Steps 1–3 — Project scaffold, supplied infrastructure/runbooks, and `uv` environment.
- [x] Steps 4–5 — Pydantic API contracts, health/triage endpoints, severity, and safety policies.
- [x] Steps 6–8 — Mock and optional PostgreSQL diagnostics, source-preserving retrieval, and triage workflow.
- [x] Steps 9–10 — All three scenarios and SQLite incident history.
- [x] Step 11 — Prometheus targets and exporter metrics verified; Grafana data source and dashboard configured and query-verified.
- [x] Step 12 — Minimal browser UI.
- [x] Step 13 — Scenario, policy, API, retrieval, and evaluation tests.
- [x] Step 14 — Read-only diagnostics, bounded input, secret redaction, and advisory-only safety review.
- [x] Step 15 — Health/API demo and monitoring service checks documented and tested.

Run the evaluation suite with `uv run pytest`. Evaluation scenarios and current
results are recorded in [docs/evaluation.md](docs/evaluation.md).
