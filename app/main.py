from fastapi import FastAPI

from app.schemas import HealthResponse

app = FastAPI(title="Incident Runbook Triage Assistant")


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    return HealthResponse(status="ok")
