import hashlib
import uuid
from pathlib import Path

import pytest
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, PointStruct, SparseVectorParams, VectorParams
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.errors import IngestionError
from app.ingestion import (
    Chunk,
    _bm25_rank,
    chunk_document,
    ensure_collection,
    extract_document,
    extract_metadata,
    index_chunks,
    reindex_documents,
    retrieve_chunks,
)
from app.models import Base, Document, Project
from app.vector import document_filter


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


@pytest.mark.asyncio
async def test_existing_collection_dimension_mismatch_is_rejected() -> None:
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    await client.create_collection("semantic", vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    settings = Settings(qdrant_collection="semantic", embedding_dimensions=384)

    try:
        with pytest.raises(IngestionError) as error:
            await ensure_collection(client, settings)

        assert error.value.code == "embedding_index_mismatch"
        assert error.value.details == {
            "collection": "semantic",
            "expected_dimensions": 384,
            "actual_dimensions": 2,
            "expected_distance": "Cosine",
            "actual_distance": "Cosine",
        }
        info = await client.get_collection("semantic")
        assert info.config.params.vectors.size == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_existing_collection_distance_mismatch_is_rejected_with_distances() -> None:
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    await client.create_collection("distance", vectors_config=VectorParams(size=2, distance=Distance.DOT))
    settings = Settings(qdrant_collection="distance", embedding_dimensions=2)

    try:
        with pytest.raises(IngestionError) as error:
            await ensure_collection(client, settings)

        assert error.value.code == "embedding_index_mismatch"
        assert error.value.details == {
            "collection": "distance",
            "expected_dimensions": 2,
            "actual_dimensions": 2,
            "expected_distance": "Cosine",
            "actual_distance": "Dot",
        }
        info = await client.get_collection("distance")
        assert info.config.params.vectors.distance == Distance.DOT
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_existing_named_vector_collection_is_rejected_with_shape_details() -> None:
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    await client.create_collection(
        "named",
        vectors_config={"content": VectorParams(size=2, distance=Distance.COSINE)},
    )
    settings = Settings(qdrant_collection="named", embedding_dimensions=2)

    try:
        with pytest.raises(IngestionError) as error:
            await ensure_collection(client, settings)

        assert error.value.code == "embedding_index_mismatch"
        assert error.value.details == {
            "collection": "named",
            "expected_vector_configuration": "unnamed",
            "actual_vector_configuration": "named",
            "actual_vector_names": ["content"],
        }
        info = await client.get_collection("named")
        assert list(info.config.params.vectors) == ["content"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_existing_sparse_only_collection_is_rejected_with_shape_details() -> None:
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    await client.create_collection(
        "sparse",
        vectors_config=None,
        sparse_vectors_config={"text": SparseVectorParams()},
    )
    settings = Settings(qdrant_collection="sparse", embedding_dimensions=2)

    try:
        with pytest.raises(IngestionError) as error:
            await ensure_collection(client, settings)

        assert error.value.code == "embedding_index_mismatch"
        assert error.value.details == {
            "collection": "sparse",
            "expected_vector_configuration": "unnamed",
            "actual_vector_configuration": "sparse_only",
            "actual_sparse_vector_names": ["text"],
        }
        info = await client.get_collection("sparse")
        assert info.config.params.vectors == {}
        assert list(info.config.params.sparse_vectors) == ["text"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_legacy_payload_remains_retrievable_before_explicit_reindex() -> None:
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    settings = Settings(embedding_dimensions=2, qdrant_collection="legacy_read")
    project_id, document_id = uuid.uuid4(), uuid.uuid4()
    try:
        await client.create_collection(
            settings.qdrant_collection,
            vectors_config=VectorParams(size=2, distance=Distance.COSINE),
        )
        await client.upsert(
            settings.qdrant_collection,
            points=[
                PointStruct(
                    id=str(uuid.uuid4()),
                    vector=[1.0, 0.0],
                    payload={
                        "project_id": str(project_id),
                        "document_id": str(document_id),
                        "document_type": "specification",
                        "filename": "legacy.md",
                        "page": 1,
                        "section": "General",
                        "text": "Legacy UPS autonomy is 15 minutes.",
                    },
                )
            ],
        )
        results = await retrieve_chunks(client, CapturingEmbedder(), settings, project_id, "UPS autonomy", 5)
    finally:
        await client.close()

    assert results[0].text == "Legacy UPS autonomy is 15 minutes."
    assert results[0].document_id == document_id


@pytest.mark.asyncio
async def test_contextual_index_returns_original_text_and_persists_parent() -> None:
    client, embedder = AsyncQdrantClient(location=":memory:", check_compatibility=False), CapturingEmbedder()
    settings = Settings(embedding_dimensions=2, qdrant_collection="contextual_index", index_version="2")
    project_id, document_id = uuid.uuid4(), uuid.uuid4()
    table = "| Parameter | Required value |\n| --- | --- |\n| Battery autonomy | 15 minutes |"
    chunk = Chunk(
        project_id,
        document_id,
        "specification",
        "UPS_Spec.md",
        2,
        "2.2 Battery",
        0,
        table,
        {
            "document_title": "UPS Specification",
            "equipment_ids": ["UPS-A"],
            "vendor_ids": ["ApexPower"],
            "revision": "B",
            "approval_status": "approved",
        },
        table,
    )
    try:
        await index_chunks(client, embedder, settings, [chunk])
        points, _ = await client.scroll(
            settings.qdrant_collection,
            scroll_filter=document_filter(project_id, document_id),
            limit=10,
            with_payload=True,
        )
        child = next(point.payload for point in points if point.payload["record_type"] == "child")
        parent = next(point.payload for point in points if point.payload["record_type"] == "parent")
        results = await retrieve_chunks(client, embedder, settings, project_id, "Battery autonomy", 5)
    finally:
        await client.close()

    expected_context = (
        "Document: UPS Specification\nType: specification\nEquipment: UPS-A\nRevision: B\n"
        f"Section: 2.2 Battery\nPage: 2\n\n{table}"
    )
    assert embedder.document_calls[0] == [expected_context]
    assert embedder.query_calls == [["Battery autonomy"]]
    assert child["original_text"] == table
    assert child["contextual_text"] == expected_context
    assert child["vendor_ids"] == ["ApexPower"]
    assert child["approval_status"] == "approved"
    assert child["index_version"] == "2"
    assert parent["original_text"] == table
    assert results[0].text == table
    assert results[0].chunk_id == str(uuid.uuid5(document_id, "0"))
    assert _bm25_rank("UPS Specification", [child])[0][1]["chunk_id"] == child["chunk_id"]


@pytest.mark.asyncio
async def test_non_contextual_evaluation_mode_embeds_original_text() -> None:
    client, embedder = AsyncQdrantClient(location=":memory:", check_compatibility=False), CapturingEmbedder()
    settings = Settings(embedding_dimensions=2, qdrant_collection="original_text_ablation")
    chunk = Chunk(
        uuid.uuid4(), uuid.uuid4(), "specification", "UPS.md", 1, "Battery", 0,
        "Autonomy shall be 15 minutes.", {"document_title": "UPS Specification"},
    )
    try:
        await index_chunks(client, embedder, settings, [chunk], contextual=False)
    finally:
        await client.close()

    assert embedder.document_calls == [[chunk.text]]


@pytest.mark.asyncio
async def test_explicit_reindex_migrates_old_document_without_changing_chunk_id(tmp_path: Path) -> None:
    source = tmp_path / "legacy.md"
    source.write_text("# UPS Battery\nUPS-A autonomy shall be 15 minutes.")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    old_settings = Settings(embedding_dimensions=2, qdrant_collection="migration", index_version="1")
    new_settings = Settings(embedding_dimensions=2, qdrant_collection="migration", index_version="2")
    embedder = CapturingEmbedder()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            project = Project(name="Migration")
            session.add(project)
            await session.flush()
            document = Document(
                project_id=project.id,
                filename=source.name,
                storage_path=str(source),
                document_type="specification",
                status="completed",
                content_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                mime_type="text/markdown",
                size_bytes=source.stat().st_size,
                metadata_json={"index_version": "1"},
            )
            session.add(document)
            await session.commit()
            extracted = extract_document(source, old_settings)
            old_chunks = chunk_document(
                extracted,
                project_id=project.id,
                document_id=document.id,
                document_type=document.document_type,
                filename=document.filename,
                attributes=extract_metadata(extracted),
            )
            await index_chunks(client, embedder, old_settings, old_chunks)
            stable_id = str(uuid.uuid5(document.id, "0"))

            migrated = await reindex_documents(session, client, embedder, new_settings, project.id)
            skipped = await reindex_documents(session, client, embedder, new_settings, project.id)
            points, _ = await client.scroll(
                new_settings.qdrant_collection,
                scroll_filter=document_filter(project.id, document.id),
                limit=10,
                with_payload=True,
            )
            child = next(point.payload for point in points if point.payload["record_type"] == "child")

            assert migrated == {"matched": 1, "reindexed": 1, "skipped": 0}
            assert skipped == {"matched": 1, "reindexed": 0, "skipped": 1}
            assert child["chunk_id"] == stable_id
            assert child["index_version"] == "2"
            assert document.metadata_json["index_version"] == "2"
    finally:
        await client.close()
        await engine.dispose()
