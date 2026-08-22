import asyncio
import hashlib
import json
import uuid
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.graph import GraphStore
from app.ingestion import (
    Chunk,
    IngestionError,
    LocalHashEmbedder,
    chunk_document,
    extract_document,
    extract_metadata,
    index_chunks,
    retrieve_chunks,
    run_ingestion,
)
from app.models import Base, Document, IngestionJob, Project
from app.main import app
from app.workflow import AnswerCitation, AnswerClaim, AnswerResult, ConversationMessage, KnowledgeService, SupportingSpan

DATASET = Path(__file__).parents[1] / "data" / "synthetic_epc"


class FakeEmbedder:
    dimensions = 8

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        terms = ["ups", "switchgear", "clearance", "delivery", "battery", "autonomy", "louvre", "crac"]
        return [
            [float(term in text.lower()) for term in terms]
            for text in texts
        ]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        terms = ["ups", "switchgear", "clearance", "delivery", "battery", "autonomy", "louvre", "crac"]
        return [
            [float(term in text.lower()) for term in terms]
            for text in texts
        ]


class FailingEmbedder:
    dimensions = 8

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise IngestionError("embedding_unavailable", "Synthetic embedding outage", 503)

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        raise IngestionError("embedding_unavailable", "Synthetic embedding outage", 503)


class FakeResponder:
    async def rewrite(self, question: str, history: list[ConversationMessage]) -> str:
        return f"{history[-1].content} {question}" if history else question

    async def answer(self, question: str, context) -> AnswerResult:
        chunk = context.chunks[0]
        return AnswerResult(
            answer=f"{chunk.text} [C1]",
            citations=[
                AnswerCitation(
                    **chunk.citation.model_dump(),
                    citation_id="C1",
                    chunk_id=chunk.chunk_id,
                    supporting_spans=[SupportingSpan(text=chunk.text, start=0, end=len(chunk.text))],
                )
            ],
            claims=[AnswerClaim(text=chunk.text, type="fact", citation_ids=["C1"])],
            confidence=1,
            status="ANSWERED",
        )


def settings(tmp_path: Path) -> Settings:
    return Settings(
        embedding_dimensions=8,
        qdrant_collection="atlas_ingestion_test",
        upload_dir=str(tmp_path / "uploads"),
        min_pdf_text_chars=10,
    )


async def test_local_hash_embedder_is_deterministic(tmp_path: Path) -> None:
    embedder = LocalHashEmbedder(settings(tmp_path))

    first = await embedder.embed_documents(["UPS battery autonomy"])
    second = await embedder.embed_queries(["UPS battery autonomy"])

    assert first == second
    assert len(first[0]) == 8


def test_synthetic_specification_extracts_metadata_and_contextual_chunks(tmp_path: Path) -> None:
    source = DATASET / "specifications" / "UPS_Specification.md"
    extracted = extract_document(source, settings(tmp_path))
    chunks = chunk_document(
        extracted,
        project_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_type="specification",
        filename=source.name,
    )

    assert extract_metadata(extracted)["equipment_tags"] == ["UPS-A"]
    assert any(chunk.page == 2 and chunk.section == "2.2 Electrical and performance requirements" for chunk in chunks)
    assert all(chunk.project_id and chunk.document_id and chunk.text for chunk in chunks)


def test_synthetic_schedule_uses_one_chunk_per_task_row(tmp_path: Path) -> None:
    source = DATASET / "schedules" / "atlas_demo_schedule.csv"
    extracted = extract_document(source, settings(tmp_path))
    chunks = chunk_document(
        extracted,
        project_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_type="schedule",
        filename=source.name,
    )

    critical = next(chunk for chunk in chunks if chunk.section == "Task T-140")
    assert len(chunks) == 14
    assert critical.page == 1
    assert "delay_days: 35" in critical.text


def test_text_pdf_extraction_does_not_invoke_ocr(tmp_path: Path) -> None:
    pdf_path = tmp_path / "text.pdf"
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Synthetic text PDF with enough extractable content for direct extraction.")
    pdf.save(pdf_path)
    pdf.close()

    extracted = extract_document(pdf_path, settings(tmp_path))

    assert extracted.pages[0].page == 1
    assert "enough extractable content" in extracted.pages[0].text


def test_graph_export_contains_all_required_synthetic_entity_types(tmp_path: Path) -> None:
    project_id = uuid.uuid4()
    graph = GraphStore(str(tmp_path / "graphs"))
    sources = [
        ("specification", DATASET / "specifications" / "UPS_Specification.md"),
        ("submittal", DATASET / "submittals" / "UPS-001_ApexPower_UPS-A.md"),
        ("RFI", DATASET / "rfis" / "RFI-003_UPS_bypass_clearance.md"),
        ("schedule", DATASET / "schedules" / "atlas_demo_schedule.csv"),
        ("commissioning_record", DATASET / "commissioning" / "UPS_Procedure_Template.md"),
    ]
    for document_type, source in sources:
        extracted = extract_document(source, settings(tmp_path))
        metadata = extract_metadata(extracted)
        document = Document(
            id=uuid.uuid4(),
            project_id=project_id,
            filename=source.name,
            document_type=document_type,
            storage_path=str(source),
            metadata_json=metadata,
        )
        graph.update(
            document,
            chunk_document(
                extracted,
                project_id=project_id,
                document_id=document.id,
                document_type=document_type,
                filename=source.name,
                attributes=metadata,
            ),
        )

    exported = graph.export(project_id)
    assert {node["type"] for node in exported["nodes"]} >= {
        "Project",
        "Document",
        "Equipment",
        "Vendor",
        "SpecificationSection",
        "RFI",
        "ScheduleTask",
        "TestProcedure",
    }
    assert json.loads((tmp_path / "graphs" / f"{project_id}.json").read_text())["project_id"] == str(project_id)


@pytest.mark.asyncio
async def test_qdrant_retrieval_is_project_filtered_and_cited(tmp_path: Path) -> None:
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    config, embedder = settings(tmp_path), FakeEmbedder()
    project_id, other_project = uuid.uuid4(), uuid.uuid4()
    first = Chunk(project_id, uuid.uuid4(), "RFI", "RFI-003.md", 1, "General", 0, "UPS bypass clearance is 900 mm.")
    second = Chunk(other_project, uuid.uuid4(), "RFI", "RFI-005.md", 1, "General", 0, "Switchgear delivery uses east louvre.")

    try:
        await index_chunks(client, embedder, config, [first, second])
        results = await retrieve_chunks(client, embedder, config, project_id, "UPS clearance", 5)
    finally:
        await client.close()

    assert len(results) == 1
    assert results[0].citation.document_id == first.document_id
    assert results[0].citation.page == 1
    assert "UPS bypass" in results[0].text


@pytest.mark.asyncio
async def test_ingestion_tracks_completion_and_failure_with_synthetic_rfi(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'atlas.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    config = settings(tmp_path)
    source = DATASET / "rfis" / "RFI-003_UPS_bypass_clearance.md"
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            project = Project(name="Synthetic integration")
            session.add(project)
            await session.flush()
            document = Document(
                project_id=project.id,
                filename=source.name,
                storage_path=str(source),
                document_type="RFI",
                status="queued",
                content_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                mime_type="text/markdown",
                size_bytes=source.stat().st_size,
                metadata_json={},
            )
            session.add(document)
            await session.flush()
            job = IngestionJob(project_id=project.id, document_id=document.id, status="queued")
            session.add(job)
            await session.commit()

            completed = await run_ingestion(session, client, FakeEmbedder(), config, document, job)
            assert completed.status == "completed"
            assert completed.chunk_count > 0
            assert document.status == "completed"

            failed_document = Document(
                project_id=project.id,
                filename="failed.md",
                storage_path=str(source),
                document_type="RFI",
                status="queued",
                content_sha256="f" * 64,
                mime_type="text/markdown",
                size_bytes=source.stat().st_size,
                metadata_json={},
            )
            session.add(failed_document)
            await session.flush()
            failed_job = IngestionJob(project_id=project.id, document_id=failed_document.id, status="queued")
            session.add(failed_job)
            await session.commit()

            with pytest.raises(IngestionError, match="Synthetic embedding outage"):
                await run_ingestion(session, client, FailingEmbedder(), config, failed_document, failed_job)
            assert failed_document.status == "failed"
            assert failed_job.status == "failed"
            assert failed_job.error == "Synthetic embedding outage"
    finally:
        await client.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_knowledge_and_rfi_workflows_match_ground_truth(tmp_path: Path) -> None:
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    config, embedder, project_id = settings(tmp_path), FakeEmbedder(), uuid.uuid4()
    truth = json.loads((DATASET / "ground_truth.json").read_text())
    try:
        sources = [
            ("specification", DATASET / "specifications" / "UPS_Specification.md"),
            ("RFI", DATASET / "rfis" / "RFI-002_UPS_battery_monitoring.md"),
            ("RFI", DATASET / "rfis" / "RFI-003_UPS_bypass_clearance.md"),
            ("RFI", DATASET / "rfis" / "RFI-005_switchgear_delivery_route.md"),
        ]
        indexed = []
        for document_type, source in sources:
            extracted = extract_document(source, config)
            metadata, document_id = extract_metadata(extracted), uuid.uuid4()
            indexed.extend(
                chunk_document(
                    extracted,
                    project_id=project_id,
                    document_id=document_id,
                    document_type=document_type,
                    filename=source.name,
                    attributes=metadata,
                )
            )
        await index_chunks(client, embedder, config, indexed)
        service = KnowledgeService(config, client, embedder, FakeResponder())
        answer = await service.copilot(
            project_id,
            truth["expected_answers"][0]["question"],
            [ConversationMessage(role="user", content="Tell me the UPS-A requirements.")],
        )
        assert "15 minutes" in answer.answer
        assert answer.rewritten_question == truth["expected_answers"][0]["question"]
        assert answer.citations[0].filename == "UPS_Specification.md"

        expected = truth["expected_duplicate_rfi_matches"]
        for item in expected:
            proposed = (DATASET / item["new_rfi"]).read_text()
            matches = await service.rfi_matches(project_id, proposed, 0.75)
            assert matches.matches[0].label == "possible previous match"
            assert matches.matches[0].citation.filename == Path(item["matching_answered_rfi"]).name
            assert item["expected_answer"] in matches.matches[0].previous_answer

        insufficient = await service.copilot(project_id, "What is the cooling tower paint color?", [])
        assert insufficient.answer == "Insufficient evidence in this project."
        assert insufficient.citations == []
    finally:
        await client.close()


def test_upload_api_ingests_synthetic_rfi_and_rejects_duplicate(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())
    try:
        with TestClient(app) as client:
            app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
            app.state.settings = settings(tmp_path)
            app.state.embedder = FakeEmbedder()
            app.state.qdrant = AsyncQdrantClient(location=":memory:", check_compatibility=False)
            app.state.graph_store = GraphStore(str(tmp_path / "api-graphs"))
            app.state.knowledge_service = KnowledgeService(
                app.state.settings, app.state.qdrant, app.state.embedder, FakeResponder()
            )
            project = client.post("/projects", json={"name": "Synthetic API project"}).json()
            source = DATASET / "rfis" / "RFI-003_UPS_bypass_clearance.md"
            with source.open("rb") as file:
                response = client.post(
                    f"/projects/{project['id']}/documents",
                    data={"document_type": "RFI"},
                    files={"file": (source.name, file, "text/markdown")},
                )
            assert response.status_code == 201
            assert response.json()["ingestion"]["status"] == "completed"
            document_id = response.json()["document"]["id"]
            retrieved = client.post(f"/projects/{project['id']}/retrieve", json={"query": "UPS clearance"})
            assert retrieved.status_code == 200
            assert retrieved.json()["results"][0]["citation"]["document_id"] == document_id
            copilot = client.post(f"/projects/{project['id']}/copilot", json={"question": "What is the UPS clearance?"})
            assert copilot.status_code == 200
            assert copilot.json()["citations"][0]["document_id"] == document_id
            proposed = (DATASET / "rfis" / "RFI-009_UPS_front_access.md").read_text()
            matches = client.post(f"/projects/{project['id']}/rfis/matches", json={"proposed_rfi": proposed})
            assert matches.status_code == 200
            assert matches.json()["matches"][0]["label"] == "possible previous match"
            graph = client.get(f"/projects/{project['id']}/graph")
            assert graph.status_code == 200
            assert any(node["type"] == "RFI" for node in graph.json()["nodes"])
            with source.open("rb") as file:
                duplicate = client.post(
                    f"/projects/{project['id']}/documents",
                    data={"document_type": "RFI"},
                    files={"file": (source.name, file, "text/markdown")},
                )
            assert duplicate.status_code == 409
    finally:
        asyncio.run(engine.dispose())
