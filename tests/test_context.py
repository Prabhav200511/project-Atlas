import uuid

import pytest

from app.config import Settings
from app.context import LexicalReranker, PostRetrievalProcessor
from app.ingestion import Citation, RetrievalResult


class FixedReranker:
    def __init__(self, scores: dict[str, float]) -> None:
        self.scores = scores

    async def score(self, query: str, texts: list[str]) -> list[float]:
        return [next((score for source, score in self.scores.items() if source in text), 0.5) for text in texts]


def result(
    text: str,
    *,
    project_id: uuid.UUID,
    filename: str = "doc.md",
    section: str = "General",
    document_id: uuid.UUID | None = None,
    parent_id: uuid.UUID | None = None,
    attributes: dict | None = None,
) -> RetrievalResult:
    document_id = document_id or uuid.uuid4()
    return RetrievalResult(
        chunk_id=str(uuid.uuid4()),
        parent_id=parent_id or uuid.uuid4(),
        document_id=document_id,
        document_type="specification",
        project_id=project_id,
        page=1,
        section=section,
        text=text,
        score=0.8,
        dense_rank=1,
        bm25_rank=1,
        rrf_score=0.03,
        citation=Citation(document_id=document_id, filename=filename, page=1, section=section),
        attributes=attributes or {},
    )


@pytest.mark.asyncio
async def test_reranks_and_selects_diverse_chunks() -> None:
    project_id = uuid.uuid4()
    first = result("# UPS\nUPS battery autonomy shall be 15 minutes.", project_id=project_id)
    duplicate = result("# UPS\nUPS battery autonomy shall be 15 minutes.", project_id=project_id)
    second = result("# Switchgear\nSwitchgear delivery is due on 15 June.", project_id=project_id)
    settings = Settings(context_min_chunks=2, context_max_chunks=2, reranker_score_threshold=0)
    processor = PostRetrievalProcessor(
        settings,
        FixedReranker({first.text: 0.8, duplicate.text: 0.99, second.text: 0.7}),
    )

    bundle = await processor.process("UPS battery delivery", project_id, [first, duplicate, second])

    assert len(bundle.chunks) == 2
    assert bundle.chunks[0].rerank_score == 0.99
    assert {chunk.document_id for chunk in bundle.chunks} != {first.document_id, duplicate.document_id}


@pytest.mark.asyncio
async def test_lexical_fallback_keeps_semantic_schedule_hit_using_structured_context() -> None:
    project_id = uuid.uuid4()
    schedule = result(
        "status: not_started\nrisk_flag: critical\ndelay_days: 35",
        project_id=project_id,
        filename="atlas_demo_schedule.csv",
        section="Task T-140",
        attributes={"document_title": "atlas_demo_schedule"},
    )
    distractor = result("An active landscaping topic.", project_id=project_id, filename="landscape.md")
    settings = Settings(context_min_chunks=1, context_max_chunks=1, reranker_score_threshold=0.3)
    processor = PostRetrievalProcessor(settings, LexicalReranker())

    bundle = await processor.process(
        "What is the active schedule risk and its downstream impact?",
        project_id,
        [distractor, schedule],
    )

    assert bundle.chunks[0].document_id == schedule.document_id
    assert bundle.chunks[0].rerank_score >= settings.reranker_score_threshold


@pytest.mark.asyncio
async def test_expands_parent_only_when_child_lacks_context() -> None:
    project_id, parent_id = uuid.uuid4(), uuid.uuid4()
    child = result("battery autonomy shall be 15 minutes.", project_id=project_id, parent_id=parent_id, section="Battery")
    heading = result("# Battery", project_id=project_id, parent_id=parent_id, section="Battery")
    calls = 0

    async def load_parent(request_project: uuid.UUID, request_parent: uuid.UUID):
        nonlocal calls
        calls += 1
        assert (request_project, request_parent) == (project_id, parent_id)
        return [heading, child]

    settings = Settings(context_min_chunks=1, context_max_chunks=1, reranker_score_threshold=0)
    processor = PostRetrievalProcessor(settings, FixedReranker({child.text: 1}), load_parent)
    bundle = await processor.process("battery autonomy", project_id, [child])

    assert calls == 1
    assert bundle.chunks[0].text.startswith("# Battery")
    assert heading.chunk_id in bundle.chunks[0].expanded_from_chunk_ids

    complete = result("# Battery\nBattery autonomy is 15 minutes.", project_id=project_id, section="Battery")
    await processor.process("battery autonomy", project_id, [complete])
    assert calls == 1


@pytest.mark.asyncio
async def test_prefers_approved_revision_and_reports_conflict() -> None:
    project_id = uuid.uuid4()
    old = result(
        "# Capacity\nUPS capacity is 500 kVA.",
        project_id=project_id,
        filename="UPS_Spec_revA.md",
        section="Capacity",
        attributes={"revision": "A", "revision_status": "superseded"},
    )
    current = result(
        "# Capacity\nUPS capacity is 600 kVA.",
        project_id=project_id,
        filename="UPS_Spec_revB.md",
        section="Capacity",
        attributes={"revision": "B", "revision_status": "approved"},
    )
    settings = Settings(context_min_chunks=1, context_max_chunks=2, reranker_score_threshold=0)
    processor = PostRetrievalProcessor(settings, FixedReranker({old.text: 0.99, current.text: 0.7}))

    bundle = await processor.process("UPS capacity", project_id, [old, current])

    assert [chunk.document_id for chunk in bundle.chunks] == [current.document_id]
    assert bundle.revision_conflicts[0].revisions == ["a", "b"]


@pytest.mark.asyncio
async def test_revision_priority_does_not_promote_an_unrelated_document_over_relevance() -> None:
    project_id = uuid.uuid4()
    proposed_recovery = result(
        "# Recovery\nThe proposed switchgear delivery recovery measure adds a second shift.",
        project_id=project_id,
        filename="CO-001.md",
        section="Recovery",
        attributes={"revision_status": "proposed"},
    )
    answered_route = result(
        "# Route\nThe answered RFI confirms the delivery gate location.",
        project_id=project_id,
        filename="RFI-005.md",
        section="Route",
        attributes={"revision_status": "answered"},
    )
    settings = Settings(context_min_chunks=1, context_max_chunks=2, reranker_score_threshold=0)
    processor = PostRetrievalProcessor(
        settings,
        FixedReranker({proposed_recovery.text: 0.9, answered_route.text: 0.2}),
    )

    bundle = await processor.process(
        "Is a switchgear delivery recovery measure approved?",
        project_id,
        [proposed_recovery, answered_route],
    )

    assert bundle.chunks[0].document_id == proposed_recovery.document_id


@pytest.mark.asyncio
async def test_compression_preserves_ids_and_enforces_token_limit() -> None:
    project_id = uuid.uuid4()
    chunks = [
        result(
            f"# UPS {index}\nUPS autonomy evidence {index} is confirmed. Unrelated landscaping sentence.",
            project_id=project_id,
        )
        for index in range(3)
    ]
    settings = Settings(
        context_min_chunks=1,
        context_max_chunks=3,
        reranker_score_threshold=0,
        max_context_tokens=30,
    )
    processor = PostRetrievalProcessor(settings, FixedReranker({chunk.text: 0.9 for chunk in chunks}))

    bundle = await processor.process("UPS autonomy evidence", project_id, chunks)

    assert bundle.total_tokens <= 30
    source = next(chunk for chunk in chunks if chunk.chunk_id == bundle.chunks[0].chunk_id)
    assert bundle.chunks[0].parent_id == source.parent_id
    assert "landscaping" not in bundle.chunks[0].text
    assert bundle.chunks[0].evidence_spans
