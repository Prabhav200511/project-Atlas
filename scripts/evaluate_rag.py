"""Deterministic baseline-versus-advanced RAG evaluation on the synthetic corpus."""

import argparse
import asyncio
import json
import math
import sys
import time
import uuid
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from statistics import fmean
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qdrant_client import AsyncQdrantClient, models

from app.config import Settings
from app.context import ContextBundle, ContextChunk, EvidenceSpan, LexicalReranker, PostRetrievalProcessor
from app.embeddings import FastEmbedder
from app.ingestion import (
    RetrievalResult,
    _payload,
    _retrieval_result,
    chunk_document,
    extract_document,
    extract_metadata,
    index_chunks,
)
from app.vector import retrieval_filter
from app.workflow import AnswerCitation, AnswerClaim, AnswerResult, KnowledgeService, SupportingSpan, _local_query_plan

ROOT = Path(__file__).parents[1]
DATASET = ROOT / "data" / "synthetic_epc"


@dataclass(frozen=True)
class Case:
    question: str
    split: str
    references: tuple[tuple[str, int], ...]
    insufficient: bool = False


class EvaluationPlanner:
    async def plan(self, project_id, query, history):
        return _local_query_plan(project_id, query, history).model_copy(update={"intent": "knowledge_query"})


class ExtractiveResponder:
    async def answer(self, question: str, context: ContextBundle) -> AnswerResult:
        if not context.chunks:
            return AnswerResult(
                answer="Insufficient evidence in this project.", confidence=0, status="INSUFFICIENT_EVIDENCE"
            )
        chunks = context.chunks[:3]
        citations, claims = [], []
        for index, chunk in enumerate(chunks, start=1):
            citation_id = f"C{index}"
            citations.append(
                AnswerCitation(
                    **chunk.citation.model_dump(),
                    citation_id=citation_id,
                    chunk_id=chunk.chunk_id,
                    supporting_spans=[SupportingSpan(text=chunk.text, start=0, end=len(chunk.text))],
                )
            )
            claims.append(AnswerClaim(text=chunk.text, type="fact", citation_ids=[citation_id]))
        return AnswerResult(
            answer="\n".join(f"{claim.text} [{claim.citation_ids[0]}]" for claim in claims),
            citations=citations,
            claims=claims,
            confidence=1,
            status="CONFLICTING_EVIDENCE" if context.revision_conflicts else "ANSWERED",
            conflicting_sources=context.revision_conflicts,
        )


def sources() -> list[tuple[str, Path]]:
    return [
        *(("specification", path) for path in sorted((DATASET / "specifications").glob("*.md"))),
        *(("submittal", path) for path in sorted((DATASET / "submittals").glob("*.md"))),
        *(("RFI", path) for path in sorted((DATASET / "rfis").glob("*.md"))),
        ("meeting_minutes", DATASET / "meeting_minutes" / "MM-014_delivery_risk_review.md"),
        ("change_order", DATASET / "change_orders" / "CO-001_switchgear_recovery.md"),
        ("schedule", DATASET / "schedules" / "atlas_demo_schedule.csv"),
        *(("commissioning_record", path) for path in sorted((DATASET / "commissioning").glob("*.md"))),
    ]


def cases() -> list[Case]:
    truth = json.loads((DATASET / "ground_truth.json").read_text())
    positive = [
        Case(
            item["question"],
            item["split"],
            tuple((Path(ref["document"]).name, int(ref.get("page", 1))) for ref in item["supporting_references"]),
        )
        for item in truth["expected_answers"]
    ]
    negative = [Case(item["question"], item["split"], (), True) for item in truth["expected_insufficient_answers"]]
    return [*positive, *negative]


async def build_index(
    settings: Settings,
    client: AsyncQdrantClient,
    project_id: uuid.UUID,
    *,
    contextual: bool = True,
):
    embedder = FastEmbedder(
        settings.embedding_model,
        settings.embedding_dimensions,
        settings.embedding_cache_dir,
    )
    catalog: dict[str, tuple[str, int]] = {}
    for document_type, path in sources():
        document_id = uuid.uuid5(project_id, path.name)
        extracted = extract_document(path, settings)
        metadata = extract_metadata(extracted)
        metadata.update({"document_title": metadata.get("title") or path.stem, "index_version": settings.index_version})
        chunks = chunk_document(
            extracted,
            project_id=project_id,
            document_id=document_id,
            document_type=document_type,
            filename=path.name,
            attributes=metadata,
        )
        await index_chunks(client, embedder, settings, chunks, contextual=contextual)
        catalog.update(
            {str(uuid.uuid5(document_id, str(chunk.chunk_index))): (path.name, chunk.page) for chunk in chunks}
        )
    return embedder, catalog


async def dense_retrieve(client, embedder, settings, project_id, query, limit=5) -> list[RetrievalResult]:
    vector = (await embedder.embed_queries([query]))[0]
    response = await client.query_points(
        collection_name=settings.qdrant_collection,
        query=vector,
        query_filter=retrieval_filter(project_id),
        limit=limit,
        with_payload=True,
    )
    return [
        _retrieval_result(_payload(point.payload, point.id), rank, None, max(float(point.score or 0), 0) * 2 / 61)
        for rank, point in enumerate(response.points, start=1)
        if (point.score or 0) > 0
    ]


def baseline_context(project_id: uuid.UUID, query: str, results: list[RetrievalResult]) -> ContextBundle:
    chunks = [
        ContextChunk(
            **item.model_dump(),
            rerank_score=item.score,
            evidence_spans=[EvidenceSpan(start=0, end=len(item.text), text=item.text)],
        )
        for item in results
    ]
    return ContextBundle(
        project_id=project_id,
        query=query,
        chunks=chunks,
        total_tokens=sum(math.ceil(len(item.text) / 4) for item in chunks),
        max_context_tokens=4_000,
    )


async def run_baseline(case: Case, client, embedder, settings, project_id):
    started = time.perf_counter()
    retrieved = await dense_retrieve(client, embedder, settings, project_id, case.question)
    context = baseline_context(project_id, case.question, retrieved)
    answer = await ExtractiveResponder().answer(case.question, context)
    return retrieved, answer, context.total_tokens, 0, (time.perf_counter() - started) * 1_000


async def run_advanced(case: Case, client, embedder, settings, project_id):
    processor = PostRetrievalProcessor(settings, reranker=LexicalReranker())
    service = KnowledgeService(
        settings,
        client,
        embedder,
        responder=ExtractiveResponder(),
        planner=EvaluationPlanner(),
        postprocessor=processor,
    )
    processor.parent_loader = service._load_parent
    started = time.perf_counter()
    answer = await service.copilot(project_id, case.question, [])
    return answer, (time.perf_counter() - started) * 1_000


def relevant_at(ranking: list[tuple[str, int]], expected: set[tuple[str, int]], k: int) -> float:
    return len(set(ranking[:k]) & expected) / len(expected) if expected else 0


def reciprocal_rank(ranking: list[tuple[str, int]], expected: set[tuple[str, int]]) -> float:
    return next((1 / rank for rank, item in enumerate(ranking, start=1) if item in expected), 0)


def summarize(rows: list[dict[str, Any]]) -> dict[str, float]:
    positives = [row for row in rows if row["expected"]]
    cited = sum(row["citation_count"] for row in positives)
    expected = sum(len(row["expected"]) for row in positives)
    claims = sum(row["claim_count"] for row in rows)
    return {
        "recall_at_5": round(fmean(row["recall_at_5"] for row in positives), 4),
        "recall_at_12": round(fmean(row["recall_at_12"] for row in positives), 4),
        "mrr": round(fmean(row["mrr"] for row in positives), 4),
        "correct_document_rate": round(fmean(row["correct_document"] for row in positives), 4),
        "correct_page_rate": round(fmean(row["correct_page"] for row in positives), 4),
        "citation_precision": round(sum(row["correct_citations"] for row in positives) / max(cited, 1), 4),
        "citation_completeness": round(sum(row["covered_references"] for row in positives) / max(expected, 1), 4),
        "unsupported_claim_rate": round(sum(row["unsupported_claims"] for row in rows) / max(claims, 1), 4),
        "insufficient_evidence_accuracy": round(fmean(row["insufficient_correct"] for row in rows), 4),
        "average_latency_ms": round(fmean(row["latency_ms"] for row in rows), 2),
        "average_input_tokens": round(fmean(row["input_tokens"] for row in rows), 2),
        "average_output_tokens": round(fmean(row["output_tokens"] for row in rows), 2),
        "corrective_retry_rate": round(fmean(row["retry_count"] > 0 for row in rows), 4),
    }


def score_row(case: Case, ranking, answer: AnswerResult, input_tokens, retry_count, latency_ms):
    expected = set(case.references)
    citation_pairs = [(item.filename, item.page) for item in answer.citations]
    expected_documents = {item[0] for item in expected}
    cited_documents = {item[0] for item in citation_pairs}
    spans = {citation.citation_id: citation.supporting_spans for citation in answer.citations}
    unsupported = sum(
        claim.support_status == "UNSUPPORTED"
        or not claim.citation_ids
        or not all(any(span.text in claim.text or claim.text in span.text for span in spans.get(value, [])) for value in claim.citation_ids)
        for claim in answer.claims
    )
    return {
        "question": case.question,
        "expected": sorted(expected),
        "retrieved": ranking,
        "recall_at_5": relevant_at(ranking, expected, 5),
        "recall_at_12": relevant_at(ranking, expected, 12),
        "mrr": reciprocal_rank(ranking, expected),
        "correct_document": bool(cited_documents & expected_documents),
        "correct_page": bool(set(citation_pairs) & expected),
        "citation_count": len(citation_pairs),
        "correct_citations": sum(item in expected for item in citation_pairs),
        "covered_references": len(set(citation_pairs) & expected),
        "claim_count": len(answer.claims),
        "unsupported_claims": unsupported,
        "insufficient_correct": (answer.status == "INSUFFICIENT_EVIDENCE") == case.insufficient,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": math.ceil(len(answer.answer) / 4),
        "retry_count": retry_count,
        "status": answer.status,
    }


async def evaluate_baseline(selected, client, embedder, settings, project_id):
    rows = []
    for case in selected:
        retrieved, answer, tokens, retry, latency = await run_baseline(case, client, embedder, settings, project_id)
        ranking = [(item.citation.filename, item.page) for item in retrieved]
        rows.append(score_row(case, ranking, answer, tokens, retry, latency))
    return {"metrics": summarize(rows), "cases": rows}


async def evaluate_contextual_retrieval(selected, client, embedder, settings, project_id):
    rows = []
    for case in selected:
        if case.insufficient:
            continue
        retrieved = await dense_retrieve(client, embedder, settings, project_id, case.question, limit=12)
        ranking = [(item.citation.filename, item.page) for item in retrieved]
        expected = set(case.references)
        rows.append(
            {
                "question": case.question,
                "recall_at_5": relevant_at(ranking, expected, 5),
                "recall_at_12": relevant_at(ranking, expected, 12),
                "mrr": reciprocal_rank(ranking, expected),
            }
        )
    return {
        "metrics": {
            name: round(fmean(row[name] for row in rows), 4)
            for name in ("recall_at_5", "recall_at_12", "mrr")
        },
        "cases": rows,
    }


async def evaluate_advanced(selected, client, embedder, settings, project_id, catalog):
    rows = []
    for case in selected:
        answer, latency = await run_advanced(case, client, embedder, settings, project_id)
        ranking = [catalog[item] for item in answer.trace.candidate_chunk_ids if item in catalog]
        rows.append(
            score_row(
                case,
                ranking,
                answer,
                answer.trace.context_tokens,
                answer.trace.retry_count,
                latency,
            )
        )
    return {"metrics": summarize(rows), "cases": rows}


async def tune(base, development, client, embedder, project_id, catalog):
    selected: dict[str, Any] = {
        "dense_retrieval_limit": 20,
        "bm25_retrieval_limit": 20,
        "rrf_dense_weight": 1.0,
        "rrf_bm25_weight": 1.0,
        "rerank_candidate_limit": 12,
        "context_max_chunks": 8,
        "reranker_score_threshold": 0.15,
    }
    candidates = {
        "dense_retrieval_limit": [10, 20],
        "bm25_retrieval_limit": [10, 20],
        "rrf_weights": [(1.0, 1.0), (1.0, 1.5), (1.5, 1.0)],
        "rerank_candidate_limit": [8, 12],
        "context_max_chunks": [5, 8],
        "reranker_score_threshold": [0.0, 0.15, 0.3],
    }
    trials = []
    for name, values in candidates.items():
        options = []
        for value in values:
            update = dict(selected)
            if name == "rrf_weights":
                update.update(rrf_dense_weight=value[0], rrf_bm25_weight=value[1])
            else:
                update[name] = value
            report = await evaluate_advanced(development, client, embedder, base.model_copy(update=update), project_id, catalog)
            metrics = report["metrics"]
            rank = (
                metrics["recall_at_5"], metrics["mrr"], metrics["citation_completeness"],
                metrics["insufficient_evidence_accuracy"], -metrics["average_latency_ms"],
            )
            options.append((rank, update, metrics))
            trials.append({"parameter": name, "value": value, "metrics": metrics})
        _, selected, _ = max(options, key=lambda item: item[0])
    return selected, trials


def markdown(report: dict[str, Any]) -> str:
    baseline, advanced = report["test"]["baseline"]["metrics"], report["test"]["advanced"]["metrics"]
    names = list(baseline)
    rows = ["| Metric | Baseline | Advanced |", "| --- | ---: | ---: |"]
    rows.extend(f"| {name.replace('_', ' ')} | {baseline[name]} | {advanced[name]} |" for name in names)
    contextual = report["contextual_retrieval_comparison"]
    retrieval_rows = ["| Retrieval metric | Original text | Contextual text |", "| --- | ---: | ---: |"]
    retrieval_rows.extend(
        f"| {name.replace('_', ' ')} | {contextual['non_contextual']['metrics'][name]} | {contextual['contextual']['metrics'][name]} |"
        for name in contextual["contextual"]["metrics"]
    )
    return "\n".join(
        [
            "# Atlas RAG Evaluation",
            "",
            "Synthetic test split; tuning used the development split only. Generation and scoring are deterministic/extractive, not LLM-judged.",
            "",
            *rows,
            "",
            "## Contextual retrieval ablation",
            "",
            "Dense retrieval over the held-out test split; the only changed input is original versus contextual chunk text.",
            "",
            *retrieval_rows,
            "",
            f"Result: {report['conclusion']}",
            "",
            f"Selected parameters: `{json.dumps(report['selected_parameters'], sort_keys=True)}`",
            "",
            f"Fusion: {report['fusion']['mode']} — {report['fusion']['reason']}",
        ]
    ) + "\n"


async def evaluate(output_dir: Path) -> dict[str, Any]:
    project_id = uuid.uuid5(uuid.NAMESPACE_DNS, "atlas-rag-evaluation")
    settings = Settings(
        embedding_dimensions=384,
        qdrant_collection="atlas_rag_evaluation",
        context_min_chunks=1,
    )
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    try:
        embedder, catalog = await build_index(settings, client, project_id)
        non_contextual_settings = settings.model_copy(
            update={"qdrant_collection": f"{settings.qdrant_collection}_non_contextual"}
        )
        non_contextual_embedder, _ = await build_index(
            non_contextual_settings,
            client,
            project_id,
            contextual=False,
        )
        truth_cases = cases()
        development = [item for item in truth_cases if item.split == "development"]
        test = [item for item in truth_cases if item.split == "test"]
        selected, trials = await tune(settings, development, client, embedder, project_id, catalog)
        tuned = settings.model_copy(update=selected)
        baseline = await evaluate_baseline(test, client, embedder, settings, project_id)
        advanced = await evaluate_advanced(test, client, embedder, tuned, project_id, catalog)
        contextual_comparison = {
            "scope": "held-out test split; dense retrieval only; identical chunks and query embedder",
            "contextual": await evaluate_contextual_retrieval(test, client, embedder, settings, project_id),
            "non_contextual": await evaluate_contextual_retrieval(
                test,
                client,
                non_contextual_embedder,
                non_contextual_settings,
                project_id,
            ),
        }
        left, right = baseline["metrics"], advanced["metrics"]
        improved = (
            right["recall_at_5"] >= left["recall_at_5"]
            and right["citation_completeness"] >= left["citation_completeness"]
            and (right["recall_at_5"] > left["recall_at_5"] or right["citation_completeness"] > left["citation_completeness"])
            and right["unsupported_claim_rate"] <= left["unsupported_claim_rate"]
        )
        weighted_supported = "weights" in getattr(models.Rrf, "model_fields", {})
        report = {
            "dataset": "SYNTHETIC EPC DEMO DATA — NOT AN OFFICIAL STANDARD OR REAL PROJECT RECORD",
            "qdrant_client_version": version("qdrant-client"),
            "fusion": {
                "qdrant_weighted_rrf_supported": weighted_supported,
                "mode": "local_weighted_rrf",
                "reason": "BM25 is a local lexical ranking and the collection has no sparse-vector index.",
            },
            "methodology": {
                "baseline": "dense retrieval top 5, then deterministic extractive generation",
                "advanced": "QueryPlan, hybrid retrieval, weighted RRF, lexical rerank, parent expansion, compression, evidence gate, grounded extractive generation, verification",
                "judge": "exact document/page/span checks; no LLM judge",
                "token_counting": "ceil(character count / 4), reported as estimated tokens",
                "development_cases": len(development),
                "test_cases": len(test),
                "contextual_ablation": "dense retrieval with contextual_text embeddings versus original_text embeddings",
            },
            "selected_parameters": selected,
            "development_tuning": trials,
            "test": {"baseline": baseline, "advanced": advanced},
            "contextual_retrieval_comparison": contextual_comparison,
            "conclusion": "Advanced RAG beat the baseline on the guarded primary metrics." if improved else "No improvement claim: advanced RAG did not beat the baseline on the guarded primary metrics.",
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "rag_evaluation.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        (output_dir / "rag_evaluation.md").write_text(markdown(report))
        return report
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    args = parser.parse_args()
    report = asyncio.run(evaluate(args.output_dir))
    print(json.dumps(report["test"], indent=2, sort_keys=True))
    print(report["conclusion"])


if __name__ == "__main__":
    main()
