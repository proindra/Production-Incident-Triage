"""Compare grounded-summary prompt versions on the fixed incident dataset."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from app.domain.rag import RAGGenerationError
from app.domain.schemas import RunbookReference, TriageRequest
from app.infrastructure.rag.config import RAGSettings
from app.infrastructure.rag.graph_builder.service import RunbookRAG
from app.infrastructure.rag.node.prompts import PROMPT_VARIANTS

_WORD = re.compile(r"[a-z0-9_]+", re.IGNORECASE)
_CITATION = re.compile(r"\[([^\[\]]+\.md(?:#[^\[\]]+)?)\]")
_IGNORED = {
    "a", "an", "and", "are", "as", "at", "be", "been", "before", "by",
    "can", "could", "do", "does", "for", "from", "has", "have", "in",
    "is", "it", "may", "might", "of", "on", "or", "should", "that",
    "the", "their", "there", "these", "this", "to", "was", "were", "will",
    "with", "would",
}


def _terms(text: str) -> set[str]:
    return {
        word.casefold()
        for word in _WORD.findall(_CITATION.sub("", text))
        if len(word) > 2 and word.casefold() not in _IGNORED
    }


def _citation_id(reference: RunbookReference) -> str:
    if reference.heading:
        return f"{reference.filename}#{reference.heading}"
    return reference.filename


def _context_relevance(
    references: list[RunbookReference],
    expected_source: str | None,
    expected_heading: str | None,
) -> float:
    if expected_source is None:
        return float(not references)
    return float(
        any(
            reference.filename == expected_source
            and reference.heading == expected_heading
            for reference in references
        )
    )


def _faithfulness(summary: str, references: list[RunbookReference]) -> float:
    summary_terms = _terms(summary)
    evidence_terms = _terms(" ".join(reference.excerpt for reference in references))
    if not summary_terms:
        return 0.0
    return len(summary_terms & evidence_terms) / len(summary_terms)


def _groundedness(summary: str, references: list[RunbookReference]) -> float:
    evidence_terms = _terms(" ".join(reference.excerpt for reference in references))
    sentences = [
        sentence
        for sentence in re.split(r"(?<=[.!?])\s+", _CITATION.sub("", summary))
        if _terms(sentence)
    ]
    if not sentences:
        return 0.0
    supported = sum(bool(_terms(sentence) & evidence_terms) for sentence in sentences)
    return supported / len(sentences)


def _load_cases(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"Evaluation dataset must be a non-empty JSON list: {path}")
    return data


def _format_score(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def evaluate(
    cases: list[dict[str, Any]],
    rag: RunbookRAG,
    prompt_versions: list[str],
) -> dict[str, Any]:
    if len(prompt_versions) < 2:
        raise ValueError("At least two prompt versions are required for comparison.")
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset_cases": len(cases),
        "metrics_note": (
            "Context relevance is exact expected source/heading retrieval accuracy. "
            "Faithfulness is content-word overlap with retrieved excerpts; it is a "
            "lexical proxy, not a semantic or human judgment."
        ),
        "prompt_versions": {},
    }

    for version in prompt_versions:
        if version not in PROMPT_VARIANTS:
            raise ValueError(f"Unknown prompt version: {version}")
        rag.generator.prompt_version = version
        results: list[dict[str, Any]] = []
        context_scores: list[float] = []
        faithfulness_scores: list[float] = []
        groundedness_scores: list[float] = []
        citation_scores: list[float] = []
        summary_failures = 0
        for case in cases:
            request = TriageRequest.model_validate(case["request"])
            query = request.incident_description
            if request.context:
                query += " " + json.dumps(request.context, sort_keys=True)
            references = rag.retrieve(query)
            expected = case["expected"]
            relevant = _context_relevance(
                references, expected.get("source"), expected.get("heading")
            )
            context_scores.append(relevant)
            result: dict[str, Any] = {
                "id": case["id"],
                "retrieved": [_citation_id(reference) for reference in references],
                "context_relevance": relevant,
                "summary": None,
                "faithfulness": None,
                "hallucination_rate": None,
                "citation_valid": None,
            }
            if expected.get("source") is None:
                results.append(result)
                continue
            try:
                generated = rag.generate_summary(
                    request.incident_description,
                    request.context or {},
                    references,
                )
            except RAGGenerationError:
                summary_failures += 1
                result["summary_error"] = "summary_rejected_or_unavailable"
                citation_scores.append(0.0)
                result["citation_valid"] = False
                results.append(result)
                continue

            faithful = _faithfulness(generated.summary, references)
            grounded = _groundedness(generated.summary, references)
            citations = _CITATION.findall(generated.summary)
            valid_citation = bool(citations) and set(citations) <= {
                _citation_id(reference) for reference in references
            }
            faithfulness_scores.append(faithful)
            groundedness_scores.append(grounded)
            citation_scores.append(float(valid_citation))
            result.update(
                {
                    "summary": generated.summary,
                    "prompt_tokens": generated.prompt_tokens,
                    "completion_tokens": generated.completion_tokens,
                    "cost_usd": generated.cost_usd,
                    "groundedness": grounded,
                    "faithfulness": faithful,
                    "hallucination_rate": 1.0 - faithful,
                    "citation_valid": valid_citation,
                }
            )
            results.append(result)

        report["prompt_versions"][version] = {
            "context_relevance": mean(context_scores) if context_scores else 0.0,
            "groundedness": (
                mean(groundedness_scores) if groundedness_scores else None
            ),
            "faithfulness": (
                mean(faithfulness_scores) if faithfulness_scores else None
            ),
            "hallucination_rate": (
                mean([1.0 - score for score in faithfulness_scores])
                if faithfulness_scores
                else None
            ),
            "citation_validity": mean(citation_scores) if citation_scores else 0.0,
            "summary_failures": summary_failures,
            "summaries_evaluated": len(faithfulness_scores),
            "prompt_tokens": sum(
                result.get("prompt_tokens", 0) or 0
                for result in results
            ),
            "completion_tokens": sum(
                result.get("completion_tokens", 0) or 0
                for result in results
            ),
            "estimated_cost_usd": (
                sum(result["cost_usd"] for result in results)
                if results
                and all(
                    result.get("cost_usd") is not None
                    for result in results
                    if result["summary"] is not None
                )
                and any(result["summary"] is not None for result in results)
                else None
            ),
            "cases": results,
        }
    baseline, candidate = prompt_versions[:2]
    metrics = (
        "context_relevance",
        "groundedness",
        "faithfulness",
        "hallucination_rate",
        "citation_validity",
    )
    maximize = {"context_relevance", "groundedness", "faithfulness", "citation_validity"}
    report["comparison"] = {
        "before_prompt": baseline,
        "after_prompt": candidate,
        "metrics": {},
    }
    for metric in metrics:
        before = report["prompt_versions"][baseline][metric]
        after = report["prompt_versions"][candidate][metric]
        delta = None if before is None or after is None else after - before
        report["comparison"]["metrics"][metric] = {
            "before": before,
            "after": after,
            "delta": delta,
            "better_prompt": (
                "insufficient_data"
                if delta is None
                else "tie"
                if delta == 0
                else candidate
                if (delta > 0) == (metric in maximize)
                else baseline
            ),
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("evaluation/scenarios.json"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("results/rag-evaluation.json"),
    )
    args = parser.parse_args()

    settings = RAGSettings.from_environment()
    settings.require_generation_settings()
    cases = _load_cases(args.dataset)
    rag = RunbookRAG.from_settings(settings)
    report = evaluate(cases, rag, list(PROMPT_VARIANTS))
    report["model"] = settings.groq_model
    report["embedding_model"] = settings.embedding_model
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in report.items() if key != "prompt_versions"}, indent=2))
    for version, metrics in report["prompt_versions"].items():
        print(
            f"{version}: context_relevance={_format_score(metrics['context_relevance'])}, "
            f"groundedness={_format_score(metrics['groundedness'])}, "
            f"faithfulness={_format_score(metrics['faithfulness'])}, "
            f"hallucination_rate={_format_score(metrics['hallucination_rate'])}, "
            f"citation_validity={_format_score(metrics['citation_validity'])}"
        )
    for metric, scores in report["comparison"]["metrics"].items():
        print(
            f"{metric}: before={_format_score(scores['before'])}, "
            f"after={_format_score(scores['after'])}, "
            f"delta={_format_score(scores['delta'])}, "
            f"better={scores['better_prompt']}"
        )
    print(f"Full report: {args.output}")


if __name__ == "__main__":
    main()
