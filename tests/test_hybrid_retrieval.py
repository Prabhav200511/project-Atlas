import uuid

import pytest
from qdrant_client import AsyncQdrantClient

from app.config import Settings
from app.ingestion import Chunk, _bm25_rank, _fuse_ranked_candidates, index_chunks, retrieve_chunks
from app.workflow import QueryPlan


class Embedder:
    dimensions = 2

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float("ups" in text.lower()), float("switchgear" in text.lower())] for text in texts]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return [[float("ups" in text.lower()), float("switchgear" in text.lower())] for text in texts]


class CapturingEmbedder:
    dimensions = 2

    def __init__(self) -> None:
        self.document_calls: list[list[str]] = []
        self.query_calls: list[list[str]] = []

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(texts)
        return [[1.0, 0.0] for _ in texts]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self.query_calls.append(texts)
        return [[1.0, 0.0] for _ in texts]


def payload(chunk_id: str, project_id: uuid.UUID, document_id: uuid.UUID, text: str) -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "parent_id": str(document_id),
        "project_id": str(project_id),
        "document_id": str(document_id),
        "document_type": "specification",
        "filename": "spec.md",
        "page": 2,
        "section": "2.2",
        "text": text,
    }


def test_bm25_preserves_exact_identifiers_and_clause_numbers() -> None:
    project_id, document_id = uuid.uuid4(), uuid.uuid4()
    ranked = _bm25_rank(
        "UPS-A clause 2.2",
        [
            payload("exact", project_id, document_id, "UPS-A is governed by clause 2.2."),
            payload("other", project_id, document_id, "UPS system requirements are in clause 3.1."),
        ],
    )

    assert ranked[0][1]["chunk_id"] == "exact"


def test_rrf_deduplicates_chunks_and_retains_ranks() -> None:
    project_id, document_id = uuid.uuid4(), uuid.uuid4()
    first = payload("first", project_id, document_id, "UPS-A clause 2.2")
    second = payload("second", project_id, document_id, "UPS-A")

    results = _fuse_ranked_candidates([(1, first), (2, second)], [(1, first)], 12)

    assert [result.chunk_id for result in results] == ["first", "second"]
    assert results[0].dense_rank == results[0].bm25_rank == 1
    assert results[0].rrf_score == pytest.approx(2 / 61)
    assert results[1].bm25_rank is None


def test_weighted_rrf_can_prefer_lexical_exact_match() -> None:
    project_id, document_id = uuid.uuid4(), uuid.uuid4()
    dense_first = payload("dense", project_id, document_id, "UPS")
    lexical_first = payload("lexical", project_id, document_id, "UPS-A clause 2.2")

    results = _fuse_ranked_candidates(
        [(1, dense_first), (2, lexical_first)],
        [(1, lexical_first)],
        12,
        dense_weight=1,
        bm25_weight=1.5,
    )

    assert results[0].chunk_id == "lexical"


@pytest.mark.asyncio
async def test_indexing_uses_document_encoder_and_retrieval_uses_query_encoder() -> None:
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    config = Settings(embedding_dimensions=2, qdrant_collection="encoder_selection")
    embedder = CapturingEmbedder()
    project_id, document_id = uuid.uuid4(), uuid.uuid4()
    chunk = Chunk(
        project_id,
        document_id,
        "specification",
        "ups.md",
        1,
        "2.2",
        0,
        "UPS-A battery autonomy.",
        {"document_title": "UPS Specification"},
    )
    query = "UPS autonomy"
    try:
        await index_chunks(client, embedder, config, [chunk])
        await retrieve_chunks(client, embedder, config, project_id, query, 1)
    finally:
        await client.close()

    assert embedder.document_calls == [[chunk.contextual_text()]]
    assert embedder.query_calls == [[query]]


@pytest.mark.asyncio
async def test_hybrid_retrieval_applies_query_plan_filters_and_project_isolation() -> None:
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    config = Settings(embedding_dimensions=2, qdrant_collection="hybrid_test")
    project_id, other_project, document_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    allowed = Chunk(project_id, document_id, "specification", "ups.md", 1, "2.2", 0, "UPS-A clause 2.2 battery autonomy.", {"equipment_tags": ["UPS-A"], "vendor": "ApexPower"})
    excluded = Chunk(project_id, uuid.uuid4(), "specification", "switchgear.md", 1, "2.2", 0, "SWGR-A clause 2.2 interrupting rating.", {"equipment_tags": ["SWGR-A"]})
    other = Chunk(other_project, uuid.uuid4(), "specification", "other.md", 1, "2.2", 0, "UPS-A clause 2.2 other project.", {"equipment_tags": ["UPS-A"]})
    plan = QueryPlan(
        original_query="UPS-A clause 2.2",
        standalone_query="UPS-A clause 2.2",
        intent="knowledge_query",
        project_id=project_id,
        document_ids=[document_id],
        equipment_ids=["UPS-A"],
        section="2.2",
        subqueries=["UPS-A clause 2.2"],
    )
    try:
        await index_chunks(client, Embedder(), config, [allowed, excluded, other])
        results = await retrieve_chunks(client, Embedder(), config, project_id, plan.standalone_query, 12, query_plan=plan)
    finally:
        await client.close()

    assert len(results) == 1
    assert results[0].project_id == project_id
    assert results[0].document_id == document_id
    assert results[0].chunk_id
    assert results[0].dense_rank == results[0].bm25_rank == 1
