from app.domain.safety import redact_secrets
from app.domain.schemas import TriageRequest
from app.services.triage import TriageEngine


def test_secret_redaction_covers_key_values_and_database_urls() -> None:
    sanitized = redact_secrets(
        "password=plain-secret postgresql://incident_user:dsn-secret@localhost:2026/incidentdb"
    )

    assert "plain-secret" not in sanitized
    assert "dsn-secret" not in sanitized
    assert "[REDACTED]" in sanitized


def test_incident_response_does_not_echo_credentials() -> None:
    result = TriageEngine().triage(
        TriageRequest(
            incident_description=(
                "Database latency is high; password is narrative-secret; "
                "postgresql://user:url-secret@localhost/db"
            )
        )
    )
    response_text = str(result.model_dump())

    assert "narrative-secret" not in response_text
    assert "url-secret" not in response_text
