"""Fixed-query, read-only PostgreSQL diagnostics."""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

from .base import DiagnosticError, DiagnosticSnapshot


_QUERIES: dict[str, tuple[tuple[str, str], ...]] = {
    "high_database_latency": (
        (
            "active_sessions",
            """
            SELECT state, wait_event_type, wait_event,
                   EXTRACT(EPOCH FROM (now() - query_start))::double precision
                       AS duration_seconds
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND state IS DISTINCT FROM 'idle'
            ORDER BY query_start NULLS LAST
            LIMIT 100
            """,
        ),
        (
            "connection_counts",
            """
            SELECT current_setting('max_connections')::integer AS max_connections,
                   count(*)::integer AS current_connections
            FROM pg_stat_activity
            """,
        ),
    ),
    "too_many_connections": (
        (
            "connection_counts",
            """
            SELECT current_setting('max_connections')::integer AS max_connections,
                   count(*)::integer AS current_connections
            FROM pg_stat_activity
            """,
        ),
        (
            "connections_by_state",
            """
            SELECT COALESCE(application_name, '(unknown)') AS application_name,
                   COALESCE(state, '(unknown)') AS state,
                   count(*)::integer AS connection_count
            FROM pg_stat_activity
            GROUP BY application_name, state
            ORDER BY connection_count DESC
            LIMIT 100
            """,
        ),
    ),
    "disk_usage_wal_retention": (
        (
            "database_sizes",
            """
            SELECT datname, pg_database_size(datname)::bigint AS bytes
            FROM pg_database
            WHERE datallowconn
            ORDER BY pg_database_size(datname) DESC
            LIMIT 20
            """,
        ),
        (
            "replication_slots",
            """
            SELECT slot_type, active, restart_lsn::text AS restart_lsn,
                   confirmed_flush_lsn::text AS confirmed_flush_lsn,
                   wal_status, safe_wal_size
            FROM pg_replication_slots
            ORDER BY slot_name
            LIMIT 100
            """,
        ),
        (
            "replication_activity",
            """
            SELECT state, sent_lsn::text AS sent_lsn, write_lsn::text AS write_lsn,
                   flush_lsn::text AS flush_lsn, replay_lsn::text AS replay_lsn
            FROM pg_stat_replication
            LIMIT 100
            """,
        ),
    ),
}


class PostgresReadOnlyDiagnostic:
    """Run a bounded set of static SELECT statements with read-only access."""

    def __init__(
        self, dsn: str, connect_timeout: int = 3, statement_timeout_ms: int = 3000
    ) -> None:
        self._dsn = dsn
        self._connect_timeout = connect_timeout
        self._statement_timeout_ms = statement_timeout_ms

    def collect(self, category: str) -> DiagnosticSnapshot:
        queries = _QUERIES.get(category)
        if queries is None:
            return DiagnosticSnapshot(unavailable=["No PostgreSQL checks exist for this category."])

        evidence: dict[str, Any] = {}
        performed: list[str] = []
        unavailable: list[str] = []
        try:
            with psycopg.connect(
                self._dsn,
                connect_timeout=self._connect_timeout,
                options=f"-c statement_timeout={self._statement_timeout_ms}",
                row_factory=dict_row,
            ) as connection:
                with connection.transaction():
                    connection.execute("SET TRANSACTION READ ONLY")
                    for label, statement in queries:
                        try:
                            with connection.transaction():
                                cursor = connection.execute(statement)
                                rows = cursor.fetchall()
                            evidence[label] = rows
                            performed.append(label)
                        except psycopg.Error:
                            unavailable.append(f"{label}: query failed or permission was insufficient.")
        except psycopg.Error as error:
            raise DiagnosticError(
                f"PostgreSQL diagnostics could not connect or start a read-only transaction ({type(error).__name__})."
            ) from None
        return DiagnosticSnapshot(evidence, performed, unavailable)
