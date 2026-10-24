# Incident Runbook Triage Assistant

This project is a safe, advisory-only assistant for investigating PostgreSQL incidents using the project runbooks.

## Project structure

```text
.
├── IMPLEMENTATION.md
├── README.md
├── docker-compose.yml
├── prometheus.yml
├── .env.example
├── .gitignore
├── requirements.txt
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

## Implementation progress

- [x] Step 1 — Create the project structure and place the existing runbooks and configuration files in their expected locations.
- [ ] Step 2 — Validate the user-provided files and review the Docker Compose and Prometheus configuration.
- [ ] Step 3 — Set up the Python environment and dependencies.
- [ ] Step 4 — Define API schemas and implement the health endpoint.
- [ ] Step 5 — Centralize severity and safety policies.
- [ ] Step 6 — Build diagnostic adapters.
- [ ] Step 7 — Implement runbook retrieval.
- [ ] Step 8 — Implement the triage workflow.
- [ ] Step 9 — Implement the three required incident scenarios.
- [ ] Step 10 — Store incident history.
- [ ] Step 11 — Connect monitoring and Grafana.
- [ ] Step 12 — Build a minimal user interface.
- [ ] Step 13 — Test and evaluate.
- [ ] Step 14 — Complete the security and reliability review.
- [ ] Step 15 — Complete the demo checklist.

Step 1 is complete. The project skeleton and user-provided files are in place; later implementation steps have not been started.
