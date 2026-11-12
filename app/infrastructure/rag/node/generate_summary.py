"""Groq generation constrained to retrieved runbook evidence."""

from __future__ import annotations

import json
import re
from typing import Any

from groq import APIError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.domain.rag import RAGGeneration, RAGGenerationError
from app.domain.safety import contains_toxicity, inspect_untrusted_text, redact_secrets
from app.domain.schemas import RunbookReference
from app.infrastructure.rag.node.prompts import (
    PROMPT_VARIANTS,
    PROMPT_VERSION,
)
from app.infrastructure.llm.resilience import (
    ProviderResilience,
    ProviderUnavailableError,
)

_CITATION = re.compile(r"\[([^\[\]]+\.md(?:#[^\[\]]+)?)\]")
_UNSAFE_REMEDIATION = re.compile(
    r"\b(?:drop\s+(?:replication\s+)?slots?|delete\s+(?:from\s+)?(?:pg_wal|wal)|"
    r"pg_terminate_backend|terminate\s+(?:sessions?|backends?)|alter\s+system)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_CAUSE_ASSERTION = re.compile(
    r"\b(?:the\s+)?(?:root\s+)?cause\s+(?:(?:is|was|has\s+been)\s+)?"
    r"(?:confirmed|proven|identified|determined|certain)\b",
    re.IGNORECASE,
)


class GroqSummaryGenerator:
    def __init__(
        self,
        model: BaseChatModel,
        input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
        provider_resilience: ProviderResilience | None = None,
        prompt_version: str = PROMPT_VERSION,
    ) -> None:
        self.model = model
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.provider_resilience = provider_resilience
        if prompt_version not in PROMPT_VARIANTS:
            raise ValueError(f"Unknown summary prompt version: {prompt_version}")
        self.prompt_version = prompt_version

    def generate(
        self,
        incident_description: str,
        context: dict[str, Any],
        references: list[RunbookReference],
    ) -> RAGGeneration:
        if not references:
            raise RAGGenerationError("No verified runbook passages were retrieved.")
        if inspect_untrusted_text(
            [incident_description, context, [reference.excerpt for reference in references]]
        ):
            raise RAGGenerationError(
                "The local model-input guard rejected untrusted incident content."
            )

        passages = [
            {
                "citation": _citation_id(reference),
                "text": redact_secrets(reference.excerpt),
            }
            for reference in references
        ]
        user_content = json.dumps(
            {
                "incident": redact_secrets(incident_description[:10_000]),
                "context": redact_secrets(json.dumps(context, sort_keys=True, default=str))[
                    :10_000
                ],
                "passages": passages,
            },
            ensure_ascii=True,
        )
        try:
            invoke = lambda: self.model.invoke(
                [
                    SystemMessage(content=PROMPT_VARIANTS[self.prompt_version]),
                    HumanMessage(content=user_content),
                ]
            )
            message = (
                self.provider_resilience.invoke(invoke)
                if self.provider_resilience is not None
                else invoke()
            )
        except ProviderUnavailableError as error:
            raise RAGGenerationError("The model provider is unavailable.") from error
        except APIError as error:
            raise RAGGenerationError("The Groq request failed.") from error

        summary = _message_text(message.content).strip()
        if (
            not summary
            or len(summary) > 2_000
            or len(summary.split()) > 120
            or _UNSAFE_REMEDIATION.search(summary)
            or _UNSUPPORTED_CAUSE_ASSERTION.search(summary)
            or contains_toxicity(summary)
        ):
            raise RAGGenerationError(
                "The generated summary was empty, too long, or contained an "
                "unsupported cause assertion, unsafe remediation, or other "
                "unsafe content."
            )
        expected_citations = {_citation_id(reference) for reference in references}
        actual_citations = set(_CITATION.findall(summary))
        if not actual_citations or not actual_citations <= expected_citations:
            raise RAGGenerationError(
                "The generated summary did not cite only retrieved passages."
            )

        usage = getattr(message, "usage_metadata", None) or {}
        prompt_tokens = _token_count(usage.get("input_tokens"))
        completion_tokens = _token_count(usage.get("output_tokens"))
        cost_usd = None
        if (
            prompt_tokens is not None
            and completion_tokens is not None
            and self.input_cost_per_million is not None
            and self.output_cost_per_million is not None
        ):
            cost_usd = (
                prompt_tokens * self.input_cost_per_million
                + completion_tokens * self.output_cost_per_million
            ) / 1_000_000
        return RAGGeneration(
            summary=summary,
            prompt_version=self.prompt_version,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
        )


def _citation_id(reference: RunbookReference) -> str:
    if reference.heading:
        return f"{reference.filename}#{reference.heading}"
    return reference.filename


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    raise RAGGenerationError("The Groq response had an unsupported content format.")


def _token_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
