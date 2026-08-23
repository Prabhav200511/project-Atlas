import uuid

import pytest
from qdrant_client import AsyncQdrantClient

from app.config import Settings
from app.embeddings import FastEmbedder
from app.ingestion import Chunk, index_chunks, retrieve_chunks


@pytest.fixture(scope="session")
def semantic_embedder(tmp_path_factory) -> FastEmbedder:
    settings = Settings(embedding_dimensions=384)
    return FastEmbedder(settings.embedding_model, 384, str(tmp_path_factory.mktemp("fastembed-model")))


@pytest.mark.asyncio
async def test_semantic_query_finds_battery_autonomy_without_keyword_overlap(semantic_embedder) -> None:
    project_id = uuid.uuid4()
    settings = Settings(
        qdrant_collection="semantic_paraphrase",
        embedding_dimensions=384,
        dense_retrieval_limit=5,
        bm25_retrieval_limit=5,
        hybrid_retrieval_limit=5,
    )
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    expected = Chunk(
        project_id,
        uuid.uuid4(),
        "specification",
        "UPS_Specification.md",
        2,
        "Battery",
        0,
        "UPS-A battery autonomy shall be 15 minutes.",
    )
    distractor = Chunk(
        project_id,
        uuid.uuid4(),
        "specification",
        "Switchgear.md",
        1,
        "Access",
        0,
        "Switchgear rear clearance shall be 900 mm.",
    )

    try:
        await index_chunks(client, semantic_embedder, settings, [expected, distractor])
        results = await retrieve_chunks(
            client,
            semantic_embedder,
            settings,
            project_id,
            "How long does backup power last during an outage?",
            5,
        )
    finally:
        await client.close()

    assert results[0].document_id == expected.document_id


@pytest.mark.asyncio
async def test_semantic_rfi_search_finds_previous_answer_by_meaning(semantic_embedder) -> None:
    project_id = uuid.uuid4()
    settings = Settings(qdrant_collection="semantic_rfi", embedding_dimensions=384)
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    previous = Chunk(
        project_id,
        uuid.uuid4(),
        "RFI",
        "RFI-003.md",
        1,
        "General",
        0,
        "The UPS bypass cabinet may be maintained from the front. **Answer:** Provide 900 mm service clearance.",
        attributes={"rfi_status": "answered"},
    )
    try:
        await index_chunks(client, semantic_embedder, settings, [previous])
        results = await retrieve_chunks(
            client,
            semantic_embedder,
            settings,
            project_id,
            "Is one-sided technician servicing permitted?",
            5,
            "RFI",
            "answered",
        )
    finally:
        await client.close()

    assert results and results[0].document_id == previous.document_id
