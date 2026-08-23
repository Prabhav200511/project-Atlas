from scripts.evaluate_synthetic import run_evaluation
from scripts.evaluate_rag import evaluate

import pytest


def test_synthetic_end_to_end_evaluation_reports_planted_results() -> None:
    result = run_evaluation()

    assert result["ingestion"] == {"uploaded_documents": 27, "completed_documents": 27}
    assert result["compliance"] == {
        "true_positive": 6,
        "false_positive": 0,
        "false_negative": 0,
        "true_negative": 6,
        "precision": 1,
        "recall": 1,
        "f1": 1,
    }
    assert result["rfi"]["recall_at_k"] == 1
    assert all(rank <= result["rfi"]["k"] for rank in result["rfi"]["expected_pair_ranks"].values())
    assert result["citation_correctness"]["rate"] == 1
    assert result["schedule"]["risk_lead_time_days"] == 35
    assert result["commissioning"] == {"coverage_percent": 100, "status": "pass"}


@pytest.mark.asyncio
async def test_rag_evaluation_uses_held_out_split_and_writes_both_reports(tmp_path) -> None:
    result = await evaluate(tmp_path)

    assert result["methodology"]["development_cases"] == 4
    assert result["methodology"]["test_cases"] == 3
    assert set(result["test"]) == {"baseline", "advanced"}
    comparison = result["contextual_retrieval_comparison"]
    assert set(comparison) == {"scope", "contextual", "non_contextual"}
    assert set(comparison["contextual"]["metrics"]) == {"recall_at_5", "recall_at_12", "mrr"}
    assert len(comparison["contextual"]["cases"]) == 2
    assert len(comparison["non_contextual"]["cases"]) == 2
    assert (tmp_path / "rag_evaluation.json").is_file()
    assert (tmp_path / "rag_evaluation.md").is_file()
    assert result["test"]["advanced"]["metrics"]["citation_precision"] >= 0.95
    if result["conclusion"].startswith("Advanced RAG beat"):
        assert result["test"]["advanced"]["metrics"]["citation_completeness"] >= result["test"]["baseline"]["metrics"]["citation_completeness"]
