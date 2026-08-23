import uuid
from pathlib import Path

import pytest
from qdrant_client import AsyncQdrantClient

from app.config import Settings
from app.context import LexicalReranker, PostRetrievalProcessor
from app.errors import IngestionError
from app.ingestion import chunk_document, extract_document, extract_metadata, index_chunks, retrieve_chunks
from app.workflow import GeminiQueryPlanner, KnowledgeService, QueryPlan


DATASET = Path(__file__).parents[1] / "data" / "synthetic_epc"


class ConstantEmbedder:
    dimensions = 8

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0] for _ in texts]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return await self.embed_documents(texts)


class UnavailableGateway:
    is_available = False


class UnavailableResponder:
    async def generate(self, *_args, **_kwargs):
        raise IngestionError("generation_unavailable", "AI provider unavailable", 503)


class ForcedStatusPlanner:
    async def plan(self, project_id, query, history):
        return QueryPlan(
            original_query=query,
            standalone_query=query,
            intent="knowledge_query",
            project_id=project_id,
            revision_status="proposed",
        )


class CapturingRfiService(KnowledgeService):
    captured_plan: QueryPlan | None = None

    async def _retrieve_answered_rfis(self, project_id: str, question: str, plan: QueryPlan):
        self.captured_plan = plan
        return []


def status_source(tmp_path: Path, status: str) -> Path:
    source = DATASET / "change_orders" / "CO-001_switchgear_recovery.md"
    if status == "proposed":
        return source
    derived = tmp_path / "CO-001_switchgear_recovery_issued_for_review.md"
    derived.write_text(
        source.read_text(encoding="utf-8").replace("**Status:** Proposed", "**Status:** Issued for review"),
        encoding="utf-8",
    )
    return derived


async def indexed_status_document(tmp_path: Path, status: str):
    project_id = uuid.uuid4()
    settings = Settings(
        embedding_dimensions=8,
        qdrant_collection=f"revision_status_{status.replace(' ', '_')}_{project_id.hex}",
        dense_retrieval_limit=8,
        bm25_retrieval_limit=8,
        hybrid_retrieval_limit=8,
        reranker_score_threshold=0,
        context_min_chunks=1,
        context_max_chunks=8,
    )
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    source = status_source(tmp_path, status)
    extracted = extract_document(source, settings)
    metadata = extract_metadata(extracted)
    document_id = uuid.uuid4()
    chunks = chunk_document(
        extracted,
        project_id=project_id,
        document_id=document_id,
        document_type="change_order",
        filename=source.name,
        attributes=metadata,
    )
    embedder = ConstantEmbedder()
    await index_chunks(client, embedder, settings, chunks)
    return client, embedder, settings, project_id, document_id, chunks


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["proposed", "issued for review"])
async def test_generic_status_is_canonical_and_retrievable_as_revision_status(tmp_path: Path, status: str) -> None:
    client, embedder, settings, project_id, document_id, chunks = await indexed_status_document(tmp_path, status)
    plan = QueryPlan(
        original_query=f"Show {status} recovery measures.",
        standalone_query=f"Show {status} recovery measures.",
        intent="knowledge_query",
        project_id=project_id,
        revision_status=status,
    )
    try:
        results = await retrieve_chunks(
            client,
            embedder,
            settings,
            project_id,
            plan.standalone_query,
            8,
            query_plan=plan,
        )
    finally:
        await client.close()

    assert chunks[0].attributes["revision_status"] == status
    assert chunks[0].attributes["approval_status"] == status
    assert results and {item.document_id for item in results} == {document_id}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "query"),
    [
        ("proposed", "Can you show proposed switchgear recovery measures?"),
        ("issued for review", "Show switchgear recovery records issued for review."),
    ],
)
async def test_provider_outage_copilot_uses_real_status_filtered_qdrant(
    tmp_path: Path,
    status: str,
    query: str,
) -> None:
    client, embedder, settings, project_id, document_id, _ = await indexed_status_document(tmp_path, status)
    service = KnowledgeService(
        settings,
        client,
        embedder,
        responder=UnavailableResponder(),
        planner=GeminiQueryPlanner(settings, UnavailableGateway()),
        postprocessor=PostRetrievalProcessor(settings, reranker=LexicalReranker()),
    )
    try:
        plan = await service.query_plan(project_id, query, [])
        answer = await service.copilot(project_id, query, [])
    finally:
        await client.close()

    assert plan.revision_status == status
    assert answer.status == "PARTIAL"
    assert answer.citations and {citation.document_id for citation in answer.citations} == {document_id}
    assert f"{status.title()} document fact:" in answer.answer


@pytest.mark.asyncio
async def test_rfi_body_matching_clears_conversational_revision_selection() -> None:
    service = CapturingRfiService(
        Settings(),
        None,
        None,
        planner=ForcedStatusPlanner(),
    )

    result = await service.rfi_matches(
        uuid.uuid4(),
        "Proposed answer: use the approved specification. What is the required rating?",
    )

    assert result.matches == []
    assert service.captured_plan is not None
    assert service.captured_plan.intent == "rfi_search"
    assert service.captured_plan.document_types == ["RFI"]
    assert service.captured_plan.revision_status is None
