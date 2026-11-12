from types import SimpleNamespace

from app.domain.rag import RAGGeneration, RAGGenerationError
from app.domain.schemas import RunbookReference
from scripts.evaluate_rag import evaluate


class FakeRAG:
    def __init__(self) -> None:
        self.generator = SimpleNamespace(prompt_version="")
        self.reference = RunbookReference(
            filename="wal.md",
            heading="Slots",
            excerpt="Replication slots can retain WAL files when consumers lag.",
        )

    def retrieve(self, query: str) -> list[RunbookReference]:
        return [] if "unknown" in query else [self.reference]

    def generate_summary(
        self, incident_description, context, references
    ) -> RAGGeneration:
        assert isinstance(incident_description, str)
        assert isinstance(context, dict)
        reference = references[0]
        citation = f"[{reference.filename}#{reference.heading}]"
        return RAGGeneration(
            summary=(
                "Replication slots can retain WAL files "
                f"{citation}"
                if self.generator.prompt_version == "grounded-summary-v2"
                else f"Pineapple causes this issue {citation}"
            ),
            prompt_version=self.generator.prompt_version,
            prompt_tokens=10,
            completion_tokens=5,
        )


def test_evaluator_compares_prompts_and_scores_retrieval_and_grounding() -> None:
    cases = [
        {
            "id": "wal",
            "request": {"incident_description": "replication slots"},
            "expected": {"source": "wal.md", "heading": "Slots"},
        },
        {
            "id": "unknown",
            "request": {"incident_description": "unknown issue"},
            "expected": {"source": None, "heading": None},
        },
    ]

    report = evaluate(
        cases,
        FakeRAG(),
        ["grounded-summary-v1", "grounded-summary-v2"],
    )

    assert report["dataset_cases"] == 2
    v1 = report["prompt_versions"]["grounded-summary-v1"]
    v2 = report["prompt_versions"]["grounded-summary-v2"]
    assert v1["context_relevance"] == 1
    assert v2["context_relevance"] == 1
    assert v1["groundedness"] < v2["groundedness"]
    assert v1["faithfulness"] < v2["faithfulness"]
    assert v2["hallucination_rate"] == 0
    assert v2["citation_validity"] == 1
    comparison = report["comparison"]["metrics"]
    assert comparison["faithfulness"]["better_prompt"] == "grounded-summary-v2"
    assert comparison["faithfulness"]["delta"] > 0


def test_evaluator_does_not_score_failed_generation_as_grounded() -> None:
    class PartiallyFailingRAG(FakeRAG):
        def generate_summary(
            self, incident_description, context, references
        ) -> RAGGeneration:
            if self.generator.prompt_version == "grounded-summary-v1":
                raise RAGGenerationError("unsafe assertion rejected")
            return super().generate_summary(
                incident_description, context, references
            )

    report = evaluate(
        [
            {
                "id": "wal",
                "request": {"incident_description": "replication slots"},
                "expected": {"source": "wal.md", "heading": "Slots"},
            }
        ],
        PartiallyFailingRAG(),
        ["grounded-summary-v1", "grounded-summary-v2"],
    )

    baseline = report["prompt_versions"]["grounded-summary-v1"]
    assert baseline["summary_failures"] == 1
    assert baseline["groundedness"] is None
    assert baseline["faithfulness"] is None
    assert baseline["hallucination_rate"] is None
    assert (
        report["comparison"]["metrics"]["faithfulness"]["better_prompt"]
        == "insufficient_data"
    )
