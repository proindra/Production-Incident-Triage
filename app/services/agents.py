"""Bounded LangGraph orchestration for triage and citation verification."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Protocol, TypedDict

from groq import APIError
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph
from opentelemetry import trace

from app.core.observability import correlation_attributes
from app.domain.safety import inspect_untrusted_text, redact_secrets
from app.domain.rag import RAGGenerationError
from app.domain.schemas import ModelUsage, RunbookReference, TriageRequest, TriageResponse
from app.infrastructure.rag.node.tool_agent_prompt import (
    PROMPT_VERSION as TOOL_AGENT_PROMPT_VERSION,
    SYSTEM_PROMPT as TOOL_AGENT_SYSTEM_PROMPT,
)
from app.infrastructure.llm.resilience import (
    ProviderResilience,
    ProviderUnavailableError,
)
from app.services.contracts import RAGService
from app.services.retrieval import RunbookRetriever

logger = logging.getLogger(__name__)

_SUPPORTED_SOURCES = {
    "high_database_latency": "high_database_latency.md",
    "too_many_connections": "too_many_connections.md",
    "disk_usage_wal_retention": "disk_usage_wal_retention.md",
}
_MAX_TOOL_CALLS = 8
_MAX_TOOL_AGENT_ROUNDS = 4


class ToolCallingModel(Protocol):
    def bind_tools(self, tools: list[StructuredTool]) -> Any: ...


class AgentState(TypedDict, total=False):
    request: dict[str, Any]
    response: TriageResponse
    references: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    model_usage: list[dict[str, Any]]
    model_safety_blocked: bool
    retry_count: int
    verification_passed: bool
    agent_steps: int
    agent_timings: list[dict[str, Any]]


class IncidentTriageGraph:
    """Two distinct agents: evidence gathering, then independent verification."""

    def __init__(
        self,
        engine: Any,
        rag_service: RAGService | None = None,
        tool_model: ToolCallingModel | None = None,
        model_name: str | None = None,
        input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
        provider_resilience: ProviderResilience | None = None,
        agent_metric_recorder: Callable[[str, str, float], None] | None = None,
    ) -> None:
        self.engine = engine
        self.retriever: RunbookRetriever = engine.retriever
        self.rag_service = rag_service
        self.tool_model = tool_model
        self.model_name = model_name
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.provider_resilience = provider_resilience
        self.agent_metric_recorder = agent_metric_recorder
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
                "model_usage": [],
                "retry_count": 0,
                "agent_steps": 0,
                "agent_timings": [],
            },
            config={"recursion_limit": 8},
        )
        return state["response"].model_copy(
            update={
                "agent_steps": state.get("agent_steps", 0),
                "tool_calls": state.get("tool_calls", []),
                "verification_passed": state.get("verification_passed", False),
                "agent_timings": state.get("agent_timings", []),
                "retrieval_retries": state.get("retry_count", 0),
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
                description=(
                    "Look up a retrieved corpus clause, including a versioned PostgreSQL "
                    "documentation section, using filename#heading text."
                ),
            ),
            "lookup_request_fields": StructuredTool.from_function(
                lookup_request_fields,
                name="lookup_request_fields",
                description="Inspect the submitted incident fields and supplied context.",
            ),
        }

    def _triage_agent(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            with self.tracer.start_as_current_span(
                "agent.triage", attributes=correlation_attributes()
            ):
                result = self._run_triage_agent(state)
        except Exception:
            self._record_agent_metric("triage", "failure", started)
            logger.exception("Triage agent step failed.")
            raise
        self._record_agent_metric("triage", "success", started)
        return _record_agent_timing(result, state, "triage", started)

    def _run_triage_agent(self, state: AgentState) -> dict[str, Any]:
        tool_state = {**state, "tool_calls": list(state.get("tool_calls", []))}
        tool_state["model_usage"] = list(state.get("model_usage", []))
        request = TriageRequest.model_validate(state["request"])
        category = self.engine._classify_for_agent(request)
        safety_flags = inspect_untrusted_text(state["request"])
        if self.tool_model is None or safety_flags:
            references = self._deterministic_retrieval(
                tool_state, state["request"], category
            )
            prompt_versions = (
                ["deterministic-policy-v1"]
                if self.tool_model is None
                else ["deterministic-policy-v1", TOOL_AGENT_PROMPT_VERSION]
            )
        else:
            prompt_versions = ["deterministic-policy-v1", TOOL_AGENT_PROMPT_VERSION]
            try:
                references = self._agent_selected_retrieval(
                    tool_state, state["request"]
                )
            except (APIError, ProviderUnavailableError) as error:
                logger.warning(
                    "Tool-selection model unavailable; using bounded local retrieval (%s).",
                    type(error).__name__,
                )
                references = self._deterministic_retrieval(
                    tool_state, state["request"], category
                )

        result = self.engine._triage_once(request, references=references)
        response_updates: dict[str, Any] = {
                "prompt_versions": prompt_versions,
                "safety_flags": safety_flags,
                "model_usage": [
                    ModelUsage.model_validate(item)
                    for item in tool_state["model_usage"]
                ],
                "estimated_cost_usd": _total_cost(tool_state["model_usage"]),
            }
        if safety_flags:
            response_updates["recommended_actions"] = [
                "Potential prompt injection or abusive content was not sent to a model; verify the incident details with a human."
            ] + result.recommended_actions
        result = result.model_copy(update=response_updates)
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
            "model_usage": tool_state["model_usage"],
            "model_safety_blocked": bool(safety_flags),
            "agent_steps": state.get("agent_steps", 0) + 1,
        }

    def _deterministic_retrieval(
        self,
        tool_state: AgentState,
        request_payload: dict[str, Any],
        category: str,
    ) -> list[RunbookReference]:
        fields = self._call_tool(
            tool_state,
            "lookup_request_fields",
            {
                "request_json": json.dumps(
                    request_payload, sort_keys=True, default=str
                )
            },
        )
        if not isinstance(fields, dict):
            raise RuntimeError("Request-field lookup returned an invalid result.")
        description = redact_secrets(str(fields["incident_description"]))
        context = fields["context"]
        query = description + " " + " ".join(
            f"{key} {value}" for key, value in context.items()
        )
        references: list[RunbookReference] = []
        if category != "unknown":
            references = self._call_tool(
                tool_state,
                "search_corpus",
                {"query": redact_secrets(query[:10_000]), "limit": 3},
            )
        if (
            category == "disk_usage_wal_retention"
            and "table" in query.casefold()
            and "stable" in query.casefold()
            and len(tool_state.get("tool_calls", [])) < _MAX_TOOL_CALLS
        ):
            clause_refs = self._call_tool(
                tool_state,
                "lookup_clause_by_id",
                {"source_id": "disk_usage_wal_retention.md#replication slots"},
            )
            references = _merge_references(references, clause_refs)
            if len(tool_state.get("tool_calls", [])) < _MAX_TOOL_CALLS:
                official_refs = self._call_tool(
                    tool_state,
                    "lookup_clause_by_id",
                    {
                        "source_id": (
                            "postgresql/17/warm-standby.md#26.2.6. Replication Slots"
                        )
                    },
                )
                references = _merge_references(references, official_refs)
        return references

    def _agent_selected_retrieval(
        self,
        tool_state: AgentState,
        request_payload: dict[str, Any],
    ) -> list[RunbookReference]:
        if self.tool_model is None:
            raise RuntimeError("Tool selection requires a configured model.")
        model = self.tool_model.bind_tools(list(self.tools.values()))
        messages = [
            SystemMessage(content=TOOL_AGENT_SYSTEM_PROMPT),
            HumanMessage(
                content=redact_secrets(
                    json.dumps(request_payload, sort_keys=True, default=str)
                )[:12_000]
            ),
        ]
        references: list[RunbookReference] = []
        for _ in range(_MAX_TOOL_AGENT_ROUNDS):
            started = time.perf_counter()
            invoke = lambda: model.invoke(messages)
            reply = (
                self.provider_resilience.invoke(invoke)
                if self.provider_resilience is not None
                else invoke()
            )
            usage = getattr(reply, "usage_metadata", None) or {}
            prompt_tokens = _token_count(usage.get("input_tokens"))
            completion_tokens = _token_count(usage.get("output_tokens"))
            cost_usd = _usage_cost(
                prompt_tokens,
                completion_tokens,
                self.input_cost_per_million,
                self.output_cost_per_million,
            )
            tool_state.setdefault("model_usage", []).append(
                ModelUsage(
                    step="tool_selection",
                    prompt_version=TOOL_AGENT_PROMPT_VERSION,
                    model=self.model_name,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=cost_usd,
                ).model_dump(mode="json")
            )
            messages.append(reply)
            tool_calls = getattr(reply, "tool_calls", [])
            if not tool_calls:
                break
            for tool_call in tool_calls:
                name = tool_call.get("name")
                arguments = tool_call.get("args")
                if name not in self.tools or not isinstance(arguments, dict):
                    raise ValueError("The tool-selection model returned an invalid tool call.")
                result = self._call_tool(tool_state, name, arguments)
                if isinstance(result, list):
                    references = _merge_references(references, result)
                    content = json.dumps(
                        [item.model_dump(mode="json") for item in result],
                        sort_keys=True,
                    )
                else:
                    content = json.dumps(result, sort_keys=True, default=str)
                messages.append(
                    ToolMessage(
                        content=content[:12_000],
                        tool_call_id=tool_call.get("id") or f"call-{len(messages)}",
                    )
                )
                if len(tool_state.get("tool_calls", [])) >= _MAX_TOOL_CALLS:
                    return references
            if time.perf_counter() - started > 60:
                raise TimeoutError("Tool-selection round exceeded its wall-clock budget.")
        return references

    def _rag_generation(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            if self.rag_service is None:
                raise RuntimeError("The RAG generation node ran while RAG is disabled.")
            response: TriageResponse = state["response"]
            request = TriageRequest.model_validate(state["request"])
            references = [
                RunbookReference.model_validate(item)
                for item in state.get("references", [])
            ]
            with self.tracer.start_as_current_span(
                "agent.rag_generation", attributes=correlation_attributes()
            ):
                try:
                    if inspect_untrusted_text(
                        [request.incident_description, request.context, references]
                    ):
                        raise RAGGenerationError(
                            "Untrusted text did not pass the local model-input guard."
                        )
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
                    usage = ModelUsage(
                        step="grounded_summary",
                        prompt_version=generated.prompt_version,
                        model=self.model_name,
                        prompt_tokens=generated.prompt_tokens,
                        completion_tokens=generated.completion_tokens,
                        cost_usd=generated.cost_usd,
                    )
                    model_usage = [*response.model_usage, usage]
                    response = response.model_copy(
                        update={
                            "rag_summary": generated.summary,
                            "rag_status": "generated",
                            "rag_warning": None,
                            "rag_prompt_version": generated.prompt_version,
                            "rag_prompt_tokens": generated.prompt_tokens,
                            "rag_completion_tokens": generated.completion_tokens,
                            "prompt_versions": list(
                                dict.fromkeys(
                                    [*response.prompt_versions, generated.prompt_version]
                                )
                            ),
                            "model_usage": model_usage,
                            "estimated_cost_usd": _total_cost(
                                [item.model_dump(mode="json") for item in model_usage]
                            ),
                        }
                    )
            result = {
                "response": response,
                "agent_steps": state.get("agent_steps", 0) + 1,
            }
        except Exception:
            self._record_agent_metric("rag_generation", "failure", started)
            logger.exception("RAG generation agent step failed.")
            raise
        self._record_agent_metric("rag_generation", "success", started)
        return _record_agent_timing(result, state, "rag_generation", started)

    def _record_agent_metric(self, agent: str, status: str, started: float) -> None:
        if self.agent_metric_recorder is not None:
            self.agent_metric_recorder(
                agent, status, (time.perf_counter() - started) * 1000
            )

    def _rag_route(self, state: AgentState) -> str:
        if (
            self.rag_service is not None
            and state.get("references")
            and not state.get("model_safety_blocked", False)
        ):
            return "generate"
        return "verify"

    def _verification_agent(self, state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            with self.tracer.start_as_current_span(
                "agent.verification", attributes=correlation_attributes()
            ):
                result = self._run_verification_agent(state)
        except Exception:
            self._record_agent_metric("verification", "failure", started)
            logger.exception("Verification agent step failed.")
            raise
        self._record_agent_metric("verification", "success", started)
        return _record_agent_timing(result, state, "verification", started)

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
        started = time.perf_counter()
        try:
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
            result = {
                "references": [reference.model_dump() for reference in references],
                "tool_calls": tool_state["tool_calls"],
                "response": self.engine._triage_once(request, references=references),
                "retry_count": state.get("retry_count", 0) + 1,
                "agent_steps": state.get("agent_steps", 0) + 1,
            }
        except Exception:
            self._record_agent_metric("alternate_search", "failure", started)
            logger.exception("Alternate-search agent step failed.")
            raise
        self._record_agent_metric("alternate_search", "success", started)
        return _record_agent_timing(result, state, "alternate_search", started)

    def _call_tool(
        self, state: AgentState, name: str, arguments: dict[str, Any]
    ) -> list[RunbookReference] | dict[str, Any]:
        calls = state.setdefault("tool_calls", [])
        if len(calls) >= _MAX_TOOL_CALLS:
            return []
        started = time.perf_counter()
        with self.tracer.start_as_current_span(
            f"tool.{name}", attributes=correlation_attributes()
        ) as span:
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
    return merged[:8]


def _token_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _usage_cost(
    prompt_tokens: int | None,
    completion_tokens: int | None,
    input_cost_per_million: float | None,
    output_cost_per_million: float | None,
) -> float | None:
    if (
        prompt_tokens is None
        or completion_tokens is None
        or input_cost_per_million is None
        or output_cost_per_million is None
    ):
        return None
    return (
        prompt_tokens * input_cost_per_million
        + completion_tokens * output_cost_per_million
    ) / 1_000_000


def _total_cost(model_usage: list[dict[str, Any]]) -> float | None:
    costs = [item.get("cost_usd") for item in model_usage]
    if not costs:
        return 0.0
    if any(not isinstance(cost, (int, float)) for cost in costs):
        return None
    return sum(costs)


def _record_agent_timing(
    result: dict[str, Any],
    state: AgentState,
    agent: str,
    started: float,
) -> dict[str, Any]:
    timings = list(state.get("agent_timings", []))
    timings.append(
        {
            "agent": agent,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "status": "success",
        }
    )
    result["agent_timings"] = timings
    return result
