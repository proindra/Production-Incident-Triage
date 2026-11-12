"""Bounded LangGraph orchestration for triage and citation verification."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, TypedDict

from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph
from opentelemetry import trace

from app.domain.safety import redact_secrets
from app.domain.rag import RAGGenerationError
from app.domain.schemas import RunbookReference, TriageRequest, TriageResponse
from app.services.contracts import RAGService
from app.services.retrieval import RunbookRetriever

logger = logging.getLogger(__name__)

_SUPPORTED_SOURCES = {
    "high_database_latency": "high_database_latency.md",
    "too_many_connections": "too_many_connections.md",
    "disk_usage_wal_retention": "disk_usage_wal_retention.md",
}
_MAX_TOOL_CALLS = 8


class AgentState(TypedDict, total=False):
    request: dict[str, Any]
    response: TriageResponse
    references: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    retry_count: int
    verification_passed: bool
    agent_steps: int


class IncidentTriageGraph:
    """Two distinct agents: evidence gathering, then independent verification."""

    def __init__(self, engine: Any, rag_service: RAGService | None = None) -> None:
        self.engine = engine
        self.retriever: RunbookRetriever = engine.retriever
        self.rag_service = rag_service
        self.tracer = trace.get_tracer("incident-triage-agent")
        self.tools = self._create_tools()

        graph = StateGraph(AgentState)
        graph.add_node("triage_agent", self._triage_agent)
        graph.add_node("rag_generation", self._rag_generation)
        graph.add_node("verification_agent", self._verification_agent)
        graph.add_node("alternate_search", self._alternate_search)
        graph.add_edge(START, "triage_agent")
        graph.add_conditional_edges(
            "triage_agent",
            self._rag_route,
            {"generate": "rag_generation", "verify": "verification_agent"},
        )
        graph.add_edge("rag_generation", "verification_agent")
        graph.add_conditional_edges(
            "verification_agent",
            self._verification_route,
            {"retry": "alternate_search", "finish": END},
        )
        graph.add_conditional_edges(
            "alternate_search",
            self._rag_route,
            {"generate": "rag_generation", "verify": "verification_agent"},
        )
        self.graph = graph.compile()

    def invoke(self, request: TriageRequest) -> TriageResponse:
        state = self.graph.invoke(
            {
                "request": request.model_dump(mode="json"),
                "references": [],
                "tool_calls": [],
                "retry_count": 0,
                "agent_steps": 0,
            },
            config={"recursion_limit": 8},
        )
        return state["response"].model_copy(
            update={
                "agent_steps": state.get("agent_steps", 0),
                "tool_calls": state.get("tool_calls", []),
                "verification_passed": state.get("verification_passed", False),
            }
        )

    def _create_tools(self) -> dict[str, StructuredTool]:
        def search_corpus(query: str, limit: int = 3) -> list[dict[str, Any]]:
            search = (
                self.rag_service.retrieve
                if self.rag_service is not None
                else self.retriever.retrieve
            )
            return [
                reference.model_dump()
                for reference in search(query, limit=limit)
            ]

        def lookup_clause_by_id(source_id: str) -> list[dict[str, Any]]:
            return [
                reference.model_dump()
                for reference in self.retriever.lookup_clause(source_id)
            ]

        def lookup_request_fields(request_json: str) -> dict[str, Any]:
            request_data = json.loads(request_json)
            context = request_data.get("context") or {}
            return {
                "incident_description": request_data.get("incident_description", ""),
                "context": context if isinstance(context, dict) else {},
            }

        return {
            "search_corpus": StructuredTool.from_function(
                search_corpus,
                name="search_corpus",
                description="Search the operational runbooks and return source excerpts.",
            ),
            "lookup_clause_by_id": StructuredTool.from_function(
                lookup_clause_by_id,
                name="lookup_clause_by_id",
                description="Look up a runbook clause using filename#heading text.",
            ),
            "lookup_request_fields": StructuredTool.from_function(
                lookup_request_fields,
                name="lookup_request_fields",
                description="Inspect the submitted incident fields and supplied context.",
            ),
        }

    def _triage_agent(self, state: AgentState) -> dict[str, Any]:
        with self.tracer.start_as_current_span("agent.triage"):
            return self._run_triage_agent(state)

    def _run_triage_agent(self, state: AgentState) -> dict[str, Any]:
        tool_state = {**state, "tool_calls": list(state.get("tool_calls", []))}
        request = TriageRequest.model_validate(state["request"])
        request_payload = json.dumps(state["request"], sort_keys=True, default=str)
        request_fields = self._call_tool(
            tool_state,
            "lookup_request_fields",
            {"request_json": request_payload},
        )
        description = redact_secrets(str(request_fields["incident_description"]))
        context = request_fields["context"]
        query = description + " " + " ".join(
            f"{key} {value}" for key, value in context.items()
        )
        category = self.engine._classify_for_agent(request)
        references: list[RunbookReference] = []
        if category != "unknown":
            references = self._call_tool(
                tool_state,
                "search_corpus",
                {"query": redact_secrets(query[:10_000]), "limit": 3},
            )
        special_case = (
            category == "disk_usage_wal_retention"
            and "table" in query.casefold()
            and "stable" in query.casefold()
        )
        if special_case and len(state.get("tool_calls", [])) < _MAX_TOOL_CALLS:
            clause_refs = self._call_tool(
                tool_state,
                "lookup_clause_by_id",
                {
                    "source_id": (
                        "disk_usage_wal_retention.md#replication slots"
                    )
                },
            )
            references = _merge_references(references, clause_refs)

        result = self.engine._triage_once(request, references=references)
        if self.rag_service is not None and not references:
            result = result.model_copy(
                update={
                    "rag_status": "skipped",
                    "rag_warning": (
                        "No supported runbook passage was retrieved; the generated "
                        "summary was skipped."
                    ),
                }
            )
        return {
            "response": result,
            "references": [reference.model_dump() for reference in references],
            "tool_calls": tool_state["tool_calls"],
            "agent_steps": state.get("agent_steps", 0) + 1,
        }

    def _rag_generation(self, state: AgentState) -> dict[str, Any]:
        if self.rag_service is None:
            raise RuntimeError("The RAG generation node ran while RAG is disabled.")
        response: TriageResponse = state["response"]
        request = TriageRequest.model_validate(state["request"])
        references = [
            RunbookReference.model_validate(item)
            for item in state.get("references", [])
        ]
        with self.tracer.start_as_current_span("agent.rag_generation"):
            try:
                generated = self.rag_service.generate_summary(
                    redact_secrets(request.incident_description),
                    request.context or {},
                    references,
                )
            except RAGGenerationError as error:
                logger.warning(
                    "RAG generation unavailable; retaining deterministic triage (%s).",
                    type(error).__name__,
                )
                response = response.model_copy(
                    update={
                        "rag_status": "unavailable",
                        "rag_warning": (
                            "Groq generation or summary verification failed; "
                            "deterministic triage is shown instead."
                        ),
                        "rag_summary": None,
                    }
                )
            else:
                response = response.model_copy(
                    update={
                        "rag_summary": generated.summary,
                        "rag_status": "generated",
                        "rag_warning": None,
                        "rag_prompt_version": generated.prompt_version,
                        "rag_prompt_tokens": generated.prompt_tokens,
                        "rag_completion_tokens": generated.completion_tokens,
                    }
                )
        return {
            "response": response,
            "agent_steps": state.get("agent_steps", 0) + 1,
        }

    def _rag_route(self, state: AgentState) -> str:
        if self.rag_service is not None and state.get("references"):
            return "generate"
        return "verify"

    def _verification_agent(self, state: AgentState) -> dict[str, Any]:
        with self.tracer.start_as_current_span("agent.verification"):
            return self._run_verification_agent(state)

    def _run_verification_agent(self, state: AgentState) -> dict[str, Any]:
        response: TriageResponse = state["response"]
        references = [
            RunbookReference.model_validate(item)
            for item in state.get("references", [])
        ]
        all_resolve = all(self.retriever.verifies(reference) for reference in references)
        expected_source = _SUPPORTED_SOURCES.get(response.category)
        has_category_source = (
            expected_source is None
            or any(reference.filename == expected_source for reference in references)
        )
        passed = all_resolve and has_category_source
        if passed or state.get("retry_count", 0) >= 1:
            if not passed and response.category != "unknown":
                response = response.model_copy(
                    update={
                        "category": "unknown",
                        "confidence": 0.0,
                        "hypotheses": [],
                        "runbook_references": [],
                        "recommended_actions": [
                            "Escalate to a human: no supporting runbook passage could be verified."
                        ],
                        "route": "page-secondary",
                        "routing_basis": (
                            "Citation verification failed; an unverified recommendation "
                            "must not be returned."
                        ),
                        "verification_passed": False,
                        "rag_summary": None,
                        "rag_status": "unavailable",
                        "rag_warning": (
                            "Retrieved citations failed verification; the result "
                            "was escalated without a generated summary."
                        ),
                        "uncertainties": [
                            *response.uncertainties,
                            "No supporting runbook passage could be verified.",
                        ],
                    }
                )
            return {
                "response": response,
                "verification_passed": passed,
                "agent_steps": state.get("agent_steps", 0) + 1,
            }
        return {"verification_passed": False}

    def _alternate_search(self, state: AgentState) -> dict[str, Any]:
        tool_state = {**state, "tool_calls": list(state.get("tool_calls", []))}
        response: TriageResponse = state["response"]
        alternate_query = (
            f"{response.category} cause evidence runbook first response "
            "supporting clause"
        )
        references = self._call_tool(
            tool_state,
            "search_corpus",
            {"query": alternate_query, "limit": 5},
        )
        expected_source = _SUPPORTED_SOURCES.get(response.category)
        if expected_source and not any(
            reference.filename == expected_source for reference in references
        ):
            clause = self._call_tool(
                tool_state,
                "lookup_clause_by_id",
                {
                    "source_id": (
                        f"{expected_source}#"
                        f"{response.category.split('_')[0]}"
                    )
                },
            )
            references = _merge_references(references, clause)
        request = TriageRequest.model_validate(state["request"])
        return {
            "references": [reference.model_dump() for reference in references],
            "tool_calls": tool_state["tool_calls"],
            "response": self.engine._triage_once(request, references=references),
            "retry_count": state.get("retry_count", 0) + 1,
            "agent_steps": state.get("agent_steps", 0) + 1,
        }

    def _call_tool(
        self, state: AgentState, name: str, arguments: dict[str, Any]
    ) -> list[RunbookReference] | dict[str, Any]:
        calls = state.setdefault("tool_calls", [])
        if len(calls) >= _MAX_TOOL_CALLS:
            return []
        started = time.perf_counter()
        with self.tracer.start_as_current_span(f"tool.{name}") as span:
            result = self.tools[name].invoke(arguments)
            span.set_attribute("tool.name", name)
            span.set_attribute("tool.result_count", len(result) if isinstance(result, list) else 1)
        calls.append(
            {
                "name": name,
                "query": redact_secrets(
                    str(arguments.get("query", arguments.get("source_id", "request fields")))
                )[:500],
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "result_count": len(result) if isinstance(result, list) else 1,
            }
        )
        if isinstance(result, list):
            return [RunbookReference.model_validate(item) for item in result]
        return result

    @staticmethod
    def _verification_route(state: AgentState) -> str:
        if state.get("verification_passed") or state.get("retry_count", 0) >= 1:
            return "finish"
        return "retry"


def _merge_references(
    existing: list[RunbookReference], additional: list[RunbookReference]
) -> list[RunbookReference]:
    merged: list[RunbookReference] = []
    seen: set[tuple[str, str | None]] = set()
    for reference in [*existing, *additional]:
        identity = (reference.filename, reference.heading)
        if identity not in seen:
            seen.add(identity)
            merged.append(reference)
    return merged[:5]
