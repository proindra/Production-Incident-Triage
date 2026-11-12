"""Groq generation constrained to retrieved runbook evidence."""

from __future__ import annotations

import json
import re
from typing import Any

from groq import APIError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.domain.rag import RAGGeneration, RAGGenerationError
from app.domain.safety import redact_secrets
from app.domain.schemas import RunbookReference
from app.infrastructure.rag.node.prompts import PROMPT_VERSION, SYSTEM_PROMPT

_CITATION = re.compile(r"\[([^\[\]]+\.md(?:#[^\[\]]+)?)\]")
_UNSAFE_REMEDIATION = re.compile(
    r"\b(?:drop\s+(?:replication\s+)?slots?|delete\s+(?:from\s+)?(?:pg_wal|wal)|"
    r"pg_terminate_backend|terminate\s+(?:sessions?|backends?)|alter\s+system)\b",
    re.IGNORECASE,
)


class GroqSummaryGenerator:
    def __init__(self, model: BaseChatModel) -> None:
        self.model = model

    def generate(
        self,
        incident_description: str,
        context: dict[str, Any],
        references: list[RunbookReference],
    ) -> RAGGeneration:
        if not references:
            raise RAGGenerationError("No verified runbook passages were retrieved.")

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
            message = self.model.invoke(
                [
                    SystemMessage(content=SYSTEM_PROMPT),
                    HumanMessage(content=user_content),
                ]
            )
        except APIError as error:
            raise RAGGenerationError("The Groq request failed.") from error

        summary = _message_text(message.content).strip()
        if (
            not summary
            or len(summary) > 2_000
            or len(summary.split()) > 120
            or _UNSAFE_REMEDIATION.search(summary)
        ):
            raise RAGGenerationError(
                "The generated summary was empty, too long, or contained unsafe remediation."
            )
        expected_citations = {_citation_id(reference) for reference in references}
        actual_citations = set(_CITATION.findall(summary))
        if not actual_citations or not actual_citations <= expected_citations:
            raise RAGGenerationError(
                "The generated summary did not cite only retrieved passages."
            )

        usage = getattr(message, "usage_metadata", None) or {}
        return RAGGeneration(
            summary=summary,
            prompt_version=PROMPT_VERSION,
            prompt_tokens=_token_count(usage.get("input_tokens")),
            completion_tokens=_token_count(usage.get("output_tokens")),
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
