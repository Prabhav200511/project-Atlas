import asyncio
import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
import app.api as api_module
from app.graph import GraphStore
from app.ingestion import (
    Chunk,
    IngestionError,
    _claim_ingestion,
    _complete_ingestion,
    _mark_failed,
    chunk_document,
    extract_document,
    extract_metadata,
    index_chunks,
    reindex_documents,
    retrieve_chunks,
    run_ingestion,
)
from app.models import AuditEvent, Base, Document, EvidenceLink, IngestionJob, Project, RFI
from app.main import app
from app.vector import document_filter
from app.workflow import AnswerCitation, AnswerClaim, AnswerResult, ConversationMessage, KnowledgeService, SupportingSpan
from scripts.seed_demo import upload_sources

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


def test_reupload_repairs_missing_source_in_place_and_preserves_document_links(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reupload.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def remove_source_and_capture_links(project_id: uuid.UUID, document_id: uuid.UUID) -> dict[str, object]:
        async with sessions() as session:
            document = await session.get(Document, document_id)
            assert document is not None
            rfi = await session.scalar(select(RFI).where(RFI.document_id == document_id))
            evidence = await session.scalar(select(EvidenceLink).where(EvidenceLink.document_id == document_id))
            assert rfi is not None
            assert evidence is not None
            audit = AuditEvent(
                project_id=project_id,
                event_type="document-reviewed",
                payload={"document_id": str(document_id), "decision": "accepted"},
            )
            session.add(audit)
            await session.commit()
            Path(document.storage_path).unlink()
            return {
                "rfi_id": rfi.id,
                "evidence_id": evidence.id,
                "audit_id": audit.id,
                "storage_path": document.storage_path,
            }

    async def surviving_state(project_id: uuid.UUID) -> dict[str, object]:
        async with sessions() as session:
            documents = list((await session.scalars(select(Document).where(Document.project_id == project_id))).all())
            rfis = list((await session.scalars(select(RFI).where(RFI.project_id == project_id))).all())
            evidence = list(
                (await session.scalars(select(EvidenceLink).where(EvidenceLink.project_id == project_id))).all()
            )
            audits = list((await session.scalars(select(AuditEvent).where(AuditEvent.project_id == project_id))).all())
            jobs = list(
                (
                    await session.scalars(
                        select(IngestionJob)
                        .where(IngestionJob.project_id == project_id)
                        .order_by(IngestionJob.attempt_number)
                    )
                ).all()
            )
            return {"documents": documents, "rfis": rfis, "evidence": evidence, "audits": audits, "jobs": jobs}

    async def force_same_timestamp_and_fail_latest(document_id: uuid.UUID, job_id: uuid.UUID) -> None:
        async with sessions() as session:
            fixed = datetime(2026, 8, 22, 9, 43, 29, tzinfo=UTC)
            await session.execute(
                update(IngestionJob).where(IngestionJob.document_id == document_id).values(created_at=fixed)
            )
            document = await session.get(Document, document_id)
            job = await session.get(IngestionJob, job_id)
            assert document is not None and job is not None
            document.status = job.status = "failed"
            await session.commit()

    asyncio.run(create_schema())
    try:
        with TestClient(app) as client:
            app.state.session_factory = sessions
            app.state.settings = settings(tmp_path)
            app.state.embedder = FakeEmbedder()
            app.state.qdrant = AsyncQdrantClient(location=":memory:", check_compatibility=False)
            app.state.graph_store = GraphStore(str(tmp_path / "reupload-graphs"))
            app.state.knowledge_service = KnowledgeService(
                app.state.settings, app.state.qdrant, app.state.embedder, FakeResponder()
            )
            project = client.post("/projects", json={"name": "Stable reupload project"}).json()
            project_id = uuid.UUID(project["id"])
            source = DATASET / "rfis" / "RFI-003_UPS_bypass_clearance.md"
            with source.open("rb") as file:
                first = client.post(
                    f"/projects/{project_id}/documents",
                    data={"document_type": "RFI"},
                    files={"file": (source.name, file, "text/markdown")},
                )
            original_document_id = uuid.UUID(first.json()["document"]["id"])
            original_job_id = uuid.UUID(first.json()["ingestion"]["id"])
            initial_graph = client.get(f"/projects/{project_id}/graph").json()
            original = asyncio.run(remove_source_and_capture_links(project_id, original_document_id))

            with source.open("rb") as file:
                repaired = client.post(
                    f"/projects/{project_id}/documents",
                    data={"document_type": "RFI"},
                    files={"file": (source.name, file, "text/markdown")},
                )

            assert repaired.status_code == 201
            assert uuid.UUID(repaired.json()["document"]["id"]) == original_document_id
            assert repaired.json()["ingestion"]["status"] == "completed"
            repair_job_id = uuid.UUID(repaired.json()["ingestion"]["id"])
            state = asyncio.run(surviving_state(project_id))
            assert [document.id for document in state["documents"]] == [original_document_id]
            assert [(item.id, item.document_id) for item in state["rfis"]] == [
                (original["rfi_id"], original_document_id)
            ]
            assert [(item.id, item.document_id) for item in state["evidence"]] == [
                (original["evidence_id"], original_document_id)
            ]
            assert [(item.id, item.payload["document_id"]) for item in state["audits"]] == [
                (original["audit_id"], str(original_document_id))
            ]
            assert [(item.id, item.attempt_number) for item in state["jobs"]] == [
                (original_job_id, 1),
                (repair_job_id, 2),
            ]
            assert Path(original["storage_path"]).is_file()
            graph = client.get(f"/projects/{project_id}/graph").json()
            assert graph == initial_graph
            logical_edges = [(edge["source"], edge["target"], edge["relation"]) for edge in graph["edges"]]
            assert len(logical_edges) == len(set(logical_edges))
            retrieved = client.post(f"/projects/{project_id}/retrieve", json={"query": "UPS clearance"})
            assert retrieved.json()["results"][0]["citation"]["document_id"] == str(original_document_id)

            asyncio.run(force_same_timestamp_and_fail_latest(original_document_id, repair_job_id))
            latest = client.get(f"/projects/{project_id}/documents/{original_document_id}/ingestion")
            assert uuid.UUID(latest.json()["ingestion"]["id"]) == repair_job_id
            assert latest.json()["ingestion"]["status"] == "failed"
            retry = client.post(f"/projects/{project_id}/documents/{original_document_id}/ingest")
            assert retry.status_code == 200
            assert uuid.UUID(retry.json()["ingestion"]["id"]) == repair_job_id
            assert retry.json()["ingestion"]["attempt_count"] == 2
            assert client.get(f"/projects/{project_id}/graph").json() == initial_graph
    finally:
        asyncio.run(engine.dispose())


def test_new_upload_failure_reports_persisted_ingestion_job_id_and_status(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'upload-failure-details.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def persisted_status(job_id: uuid.UUID) -> str | None:
        async with sessions() as session:
            job = await session.get(IngestionJob, job_id)
            return job.status if job else None

    asyncio.run(create_schema())
    try:
        with TestClient(app) as client:
            app.state.session_factory = sessions
            app.state.settings = settings(tmp_path)
            app.state.embedder = FailingEmbedder()
            app.state.qdrant = AsyncQdrantClient(location=":memory:", check_compatibility=False)
            app.state.graph_store = GraphStore(str(tmp_path / "upload-failure-details-graphs"))
            app.state.knowledge_service = KnowledgeService(
                app.state.settings, app.state.qdrant, app.state.embedder, FakeResponder()
            )
            project = client.post("/projects", json={"name": "Upload failure details"}).json()
            source = DATASET / "rfis" / "RFI-003_UPS_bypass_clearance.md"

            response = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": (source.name, source.read_bytes(), "text/markdown")},
            )

            assert response.status_code == 503
            details = response.json()["error"]["details"]
            job_id = uuid.UUID(details["ingestion_job_id"])
            assert details["status"] == "failed"
            assert asyncio.run(persisted_status(job_id)) == "failed"
    finally:
        asyncio.run(engine.dispose())


def test_repair_rejects_changed_content_and_type_without_mutating_stable_document(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'repair-integrity.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def snapshot(document_id: uuid.UUID) -> dict[str, object]:
        async with sessions() as session:
            document = await session.get(Document, document_id)
            assert document is not None
            jobs = list((await session.scalars(select(IngestionJob).where(IngestionJob.document_id == document_id))).all())
            rfi = await session.scalar(select(RFI).where(RFI.document_id == document_id))
            evidence = await session.scalar(select(EvidenceLink).where(EvidenceLink.document_id == document_id))
            assert rfi is not None and evidence is not None
            return {
                "document": (
                    document.id,
                    document.document_type,
                    document.content_sha256,
                    document.mime_type,
                    document.size_bytes,
                    document.metadata_json,
                ),
                "job_ids": [job.id for job in jobs],
                "rfi_id": rfi.id,
                "evidence_id": evidence.id,
                "storage_path": document.storage_path,
            }

    asyncio.run(create_schema())
    try:
        with TestClient(app) as client:
            app.state.session_factory = sessions
            app.state.settings = settings(tmp_path)
            app.state.embedder = FakeEmbedder()
            app.state.qdrant = AsyncQdrantClient(location=":memory:", check_compatibility=False)
            app.state.graph_store = GraphStore(str(tmp_path / "integrity-graphs"))
            app.state.knowledge_service = KnowledgeService(
                app.state.settings, app.state.qdrant, app.state.embedder, FakeResponder()
            )
            project = client.post("/projects", json={"name": "Repair integrity"}).json()
            source = DATASET / "rfis" / "RFI-003_UPS_bypass_clearance.md"
            original_content = source.read_bytes()
            first = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": (source.name, original_content, "text/markdown")},
            )
            document_id = uuid.UUID(first.json()["document"]["id"])
            before = asyncio.run(snapshot(document_id))
            before_graph = client.get(f"/projects/{project['id']}/graph").json()
            points_before, _ = asyncio.run(
                app.state.qdrant.scroll(
                    app.state.settings.qdrant_collection,
                    scroll_filter=document_filter(uuid.UUID(project["id"]), document_id),
                    limit=100,
                    with_payload=True,
                )
            )
            canonical = Path(before["storage_path"])
            corrupted = b"corrupted deployment-local bytes"
            canonical.write_bytes(corrupted)

            changed = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": (source.name, b"# Changed RFI\nDifferent logical source.", "text/markdown")},
            )
            wrong_type = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "specification"},
                files={"file": (source.name, original_content, "text/markdown")},
            )
            wrong_mime = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": (source.name, original_content, "application/pdf")},
            )

            assert changed.status_code == 409
            assert changed.json()["error"]["code"] == "document_repair_mismatch"
            assert wrong_type.status_code == 409
            assert wrong_type.json()["error"]["code"] == "document_repair_mismatch"
            assert wrong_mime.status_code == 409
            assert wrong_mime.json()["error"]["code"] == "document_repair_mismatch"
            assert canonical.read_bytes() == corrupted
            assert asyncio.run(snapshot(document_id)) == before
            assert client.get(f"/projects/{project['id']}/graph").json() == before_graph
            points_after, _ = asyncio.run(
                app.state.qdrant.scroll(
                    app.state.settings.qdrant_collection,
                    scroll_filter=document_filter(uuid.UUID(project["id"]), document_id),
                    limit=100,
                    with_payload=True,
                )
            )
            assert [(point.id, point.payload) for point in points_after] == [
                (point.id, point.payload) for point in points_before
            ]
    finally:
        asyncio.run(engine.dispose())


def test_legacy_same_filename_repair_selects_exact_identity_and_rejects_ambiguity(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-filename.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def seed_legacy_rows(project_id: uuid.UUID, other_project_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
        target_content = b"# RFI\nExact legacy logical source."
        decoy_content = b"# RFI\nDifferent legacy logical source."
        cross_project_content = target_content
        target_id, decoy_id = uuid.uuid4(), uuid.uuid4()
        async with sessions() as session:
            rows = [
                Document(
                    id=decoy_id,
                    project_id=project_id,
                    filename="legacy.md",
                    storage_path=str(tmp_path / "decoy.md"),
                    document_type="RFI",
                    status="completed",
                    content_sha256=hashlib.sha256(decoy_content).hexdigest(),
                    mime_type="text/markdown",
                    size_bytes=len(decoy_content),
                    metadata_json={},
                ),
                Document(
                    id=target_id,
                    project_id=project_id,
                    filename="legacy.md",
                    storage_path=str(tmp_path / "target.md"),
                    document_type="RFI",
                    status="failed",
                    content_sha256=hashlib.sha256(target_content).hexdigest(),
                    mime_type="text/markdown",
                    size_bytes=len(target_content),
                    metadata_json={},
                ),
                Document(
                    project_id=other_project_id,
                    filename="legacy.md",
                    storage_path=str(tmp_path / "cross-project.md"),
                    document_type="RFI",
                    status="failed",
                    content_sha256=hashlib.sha256(cross_project_content).hexdigest(),
                    mime_type="text/markdown",
                    size_bytes=len(cross_project_content),
                    metadata_json={},
                ),
            ]
            session.add_all(rows)
            await session.flush()
            session.add_all(
                IngestionJob(project_id=row.project_id, document_id=row.id, attempt_number=1, status=row.status)
                for row in rows
            )
            await session.commit()
        return target_id, decoy_id

    async def snapshot(project_id: uuid.UUID) -> list[tuple[uuid.UUID, str, str, int]]:
        async with sessions() as session:
            documents = list(
                (await session.scalars(select(Document).where(Document.project_id == project_id))).all()
            )
            result = []
            for document in documents:
                job_count = len(
                    (await session.scalars(select(IngestionJob).where(IngestionJob.document_id == document.id))).all()
                )
                result.append((document.id, document.status, document.content_sha256, job_count))
            return sorted(result, key=lambda item: str(item[0]))

    asyncio.run(create_schema())
    try:
        with TestClient(app) as client:
            app.state.session_factory = sessions
            app.state.settings = settings(tmp_path)
            app.state.embedder = FakeEmbedder()
            app.state.qdrant = AsyncQdrantClient(location=":memory:", check_compatibility=False)
            app.state.graph_store = GraphStore(str(tmp_path / "legacy-filename-graphs"))
            app.state.knowledge_service = KnowledgeService(
                app.state.settings, app.state.qdrant, app.state.embedder, FakeResponder()
            )
            project = client.post("/projects", json={"name": "Legacy identity"}).json()
            other_project = client.post("/projects", json={"name": "Other project"}).json()
            project_id = uuid.UUID(project["id"])
            other_project_id = uuid.UUID(other_project["id"])
            target_id, decoy_id = asyncio.run(seed_legacy_rows(project_id, other_project_id))
            cross_project_before = asyncio.run(snapshot(other_project_id))

            repaired = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": ("legacy.md", b"# RFI\nExact legacy logical source.", "text/markdown")},
            )

            assert repaired.status_code == 201
            assert uuid.UUID(repaired.json()["document"]["id"]) == target_id
            assert (tmp_path / "target.md").read_bytes() == b"# RFI\nExact legacy logical source."
            after_exact = asyncio.run(snapshot(project_id))
            assert len(after_exact) == 2
            assert next(item for item in after_exact if item[0] == decoy_id)[3] == 1
            assert asyncio.run(snapshot(other_project_id)) == cross_project_before

            before_ambiguous = asyncio.run(snapshot(project_id))
            ambiguous = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": ("legacy.md", b"# RFI\nA third logical source.", "text/markdown")},
            )
            assert ambiguous.status_code == 409
            assert ambiguous.json()["error"]["code"] == "document_repair_ambiguous"
            assert asyncio.run(snapshot(project_id)) == before_ambiguous
            assert asyncio.run(snapshot(other_project_id)) == cross_project_before
    finally:
        asyncio.run(engine.dispose())


def test_repair_restores_corrupted_existing_source_with_identical_content(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'corrupt-repair.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def storage_path(document_id: uuid.UUID) -> Path:
        async with sessions() as session:
            document = await session.get(Document, document_id)
            assert document is not None
            return Path(document.storage_path)

    asyncio.run(create_schema())
    try:
        with TestClient(app) as client:
            app.state.session_factory = sessions
            app.state.settings = settings(tmp_path)
            app.state.embedder = FakeEmbedder()
            app.state.qdrant = AsyncQdrantClient(location=":memory:", check_compatibility=False)
            app.state.graph_store = GraphStore(str(tmp_path / "corrupt-graphs"))
            app.state.knowledge_service = KnowledgeService(
                app.state.settings, app.state.qdrant, app.state.embedder, FakeResponder()
            )
            project = client.post("/projects", json={"name": "Corrupt source recovery"}).json()
            source = DATASET / "rfis" / "RFI-003_UPS_bypass_clearance.md"
            original_content = source.read_bytes()
            first = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": (source.name, original_content, "text/markdown")},
            )
            document_id = uuid.UUID(first.json()["document"]["id"])
            canonical = asyncio.run(storage_path(document_id))
            canonical.write_bytes(b"truncated")

            repaired = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": (source.name, original_content, "text/markdown")},
            )

            assert repaired.status_code == 201
            assert uuid.UUID(repaired.json()["document"]["id"]) == document_id
            assert canonical.read_bytes() == original_content
    finally:
        asyncio.run(engine.dispose())


def test_concurrent_same_document_repairs_are_serialized_in_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'concurrent-repair.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def state(document_id: uuid.UUID) -> tuple[uuid.UUID, list[int], str]:
        async with sessions() as session:
            document = await session.get(Document, document_id)
            assert document is not None
            jobs = list(
                (await session.scalars(select(IngestionJob).where(IngestionJob.document_id == document_id))).all()
            )
            return document.id, sorted(job.attempt_number for job in jobs), document.status

    asyncio.run(create_schema())
    try:
        with TestClient(app) as client:
            app.state.session_factory = sessions
            app.state.settings = settings(tmp_path)
            app.state.embedder = FakeEmbedder()
            app.state.qdrant = AsyncQdrantClient(location=":memory:", check_compatibility=False)
            app.state.graph_store = GraphStore(str(tmp_path / "concurrent-graphs"))
            app.state.knowledge_service = KnowledgeService(
                app.state.settings, app.state.qdrant, app.state.embedder, FakeResponder()
            )
            project = client.post("/projects", json={"name": "Concurrent repair"}).json()
            source = DATASET / "rfis" / "RFI-003_UPS_bypass_clearance.md"
            content = source.read_bytes()
            first = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": (source.name, content, "text/markdown")},
            )
            document_id = uuid.UUID(first.json()["document"]["id"])
            canonical = Path(app.state.settings.upload_dir) / project["id"] / str(document_id) / source.name
            canonical.unlink()

            original_stage = api_module._stage_repair
            staged = threading.Barrier(2)

            def synchronized_stage(path: Path, body: bytes, expected_hash: str) -> Path:
                result = original_stage(path, body, expected_hash)
                staged.wait(timeout=5)
                return result

            monkeypatch.setattr(api_module, "_stage_repair", synchronized_stage)

            def repair() -> tuple[int, str | None]:
                response = client.post(
                    f"/projects/{project['id']}/documents",
                    data={"document_type": "RFI"},
                    files={"file": (source.name, content, "text/markdown")},
                )
                return response.status_code, response.json().get("error", {}).get("code")

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = [future.result() for future in [executor.submit(repair), executor.submit(repair)]]

            assert sorted(status for status, _ in results) == [201, 409]
            assert {code for status, code in results if status == 409} <= {
                "duplicate_document",
                "document_repair_in_progress",
            }
            assert asyncio.run(state(document_id)) == (document_id, [1, 2], "completed")
            assert canonical.read_bytes() == content
    finally:
        asyncio.run(engine.dispose())


def test_repair_write_failure_preserves_canonical_and_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'write-failure.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(create_schema())
    try:
        with TestClient(app) as client:
            app.state.session_factory = sessions
            app.state.settings = settings(tmp_path)
            app.state.embedder = FakeEmbedder()
            app.state.qdrant = AsyncQdrantClient(location=":memory:", check_compatibility=False)
            app.state.graph_store = GraphStore(str(tmp_path / "write-failure-graphs"))
            app.state.knowledge_service = KnowledgeService(
                app.state.settings, app.state.qdrant, app.state.embedder, FakeResponder()
            )
            project = client.post("/projects", json={"name": "Write failure"}).json()
            source = DATASET / "rfis" / "RFI-003_UPS_bypass_clearance.md"
            content = source.read_bytes()
            first = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": (source.name, content, "text/markdown")},
            )
            document_id = uuid.UUID(first.json()["document"]["id"])
            canonical = Path(app.state.settings.upload_dir) / project["id"] / str(document_id) / source.name
            corrupted = b"pre-existing corrupted bytes"
            canonical.write_bytes(corrupted)

            original_stage = api_module._stage_repair

            def fail_stage(*_: object) -> Path:
                raise OSError("simulated disk failure")

            monkeypatch.setattr(api_module, "_stage_repair", fail_stage)
            failed = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": (source.name, content, "text/markdown")},
            )
            assert failed.status_code == 500
            assert failed.json()["error"]["code"] == "upload_storage_failed"
            assert canonical.read_bytes() == corrupted

            monkeypatch.setattr(api_module, "_stage_repair", original_stage)
            retried = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": (source.name, content, "text/markdown")},
            )
            assert retried.status_code == 201, retried.json()
            assert uuid.UUID(retried.json()["document"]["id"]) == document_id
            assert canonical.read_bytes() == content
    finally:
        asyncio.run(engine.dispose())


def test_repair_database_failure_never_unlinks_canonical_or_strands_document(tmp_path: Path) -> None:
    class FailingRepairSession(AsyncSession):
        fail_repair_commit = False

        async def commit(self) -> None:
            if self.fail_repair_commit and any(
                isinstance(item, IngestionJob) and item.attempt_number == 2
                for item in self.identity_map.values()
            ):
                type(self).fail_repair_commit = False
                raise RuntimeError("simulated database commit failure")
            await super().commit()

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'database-failure.db'}")
    sessions = async_sessionmaker(engine, class_=FailingRepairSession, expire_on_commit=False)

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def database_state(document_id: uuid.UUID) -> tuple[uuid.UUID, str, list[int]]:
        async with sessions() as session:
            document = await session.get(Document, document_id)
            assert document is not None
            jobs = list(
                (await session.scalars(select(IngestionJob).where(IngestionJob.document_id == document_id))).all()
            )
            return document.id, document.status, [job.attempt_number for job in jobs]

    asyncio.run(create_schema())
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            app.state.session_factory = sessions
            app.state.settings = settings(tmp_path)
            app.state.embedder = FakeEmbedder()
            app.state.qdrant = AsyncQdrantClient(location=":memory:", check_compatibility=False)
            app.state.graph_store = GraphStore(str(tmp_path / "database-failure-graphs"))
            app.state.knowledge_service = KnowledgeService(
                app.state.settings, app.state.qdrant, app.state.embedder, FakeResponder()
            )
            project = client.post("/projects", json={"name": "Database failure"}).json()
            source = DATASET / "rfis" / "RFI-003_UPS_bypass_clearance.md"
            content = source.read_bytes()
            first = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": (source.name, content, "text/markdown")},
            )
            document_id = uuid.UUID(first.json()["document"]["id"])
            canonical = Path(app.state.settings.upload_dir) / project["id"] / str(document_id) / source.name
            canonical.write_bytes(b"pre-existing corrupted bytes")
            FailingRepairSession.fail_repair_commit = True

            failed = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": (source.name, content, "text/markdown")},
            )

            assert failed.status_code == 500
            assert canonical.read_bytes() == content
            assert asyncio.run(database_state(document_id)) == (document_id, "completed", [1])
            retry = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": (source.name, content, "text/markdown")},
            )
            assert retry.status_code == 409
            assert retry.json()["error"]["code"] == "duplicate_document"
            assert asyncio.run(database_state(document_id)) == (document_id, "completed", [1])
    finally:
        asyncio.run(engine.dispose())


def test_seed_reupload_is_end_to_end_restartable_with_healthy_first_and_missing_later(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'seed-reupload.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def document_state(project_id: uuid.UUID) -> dict[str, tuple[uuid.UUID, Path, list[int]]]:
        async with sessions() as session:
            documents = list(
                (await session.scalars(select(Document).where(Document.project_id == project_id))).all()
            )
            result = {}
            for document in documents:
                attempts = list(
                    (
                        await session.scalars(
                            select(IngestionJob.attempt_number)
                            .where(IngestionJob.document_id == document.id)
                            .order_by(IngestionJob.attempt_number)
                        )
                    ).all()
                )
                result[document.filename] = (document.id, Path(document.storage_path), attempts)
            return result

    asyncio.run(create_schema())
    try:
        with TestClient(app) as client:
            app.state.session_factory = sessions
            app.state.settings = settings(tmp_path)
            app.state.embedder = FakeEmbedder()
            app.state.qdrant = AsyncQdrantClient(location=":memory:", check_compatibility=False)
            app.state.graph_store = GraphStore(str(tmp_path / "seed-reupload-graphs"))
            app.state.knowledge_service = KnowledgeService(
                app.state.settings, app.state.qdrant, app.state.embedder, FakeResponder()
            )
            project = client.post("/projects", json={"name": "Seed restartability"}).json()
            project_id = uuid.UUID(project["id"])
            source_list = [
                ("specification", DATASET / "specifications" / "UPS_Specification.md"),
                ("RFI", DATASET / "rfis" / "RFI-003_UPS_bypass_clearance.md"),
            ]
            upload_sources(client, project["id"], source_list, set(), reupload=False)
            before = asyncio.run(document_state(project_id))
            before[source_list[1][1].name][1].unlink()

            upload_sources(
                client,
                project["id"],
                source_list,
                {path.name for _, path in source_list},
                reupload=True,
            )
            after_repair = asyncio.run(document_state(project_id))
            upload_sources(
                client,
                project["id"],
                source_list,
                {path.name for _, path in source_list},
                reupload=True,
            )
            after_retry = asyncio.run(document_state(project_id))

            assert after_repair[source_list[0][1].name][0] == before[source_list[0][1].name][0]
            assert after_repair[source_list[0][1].name][2] == [1]
            assert after_repair[source_list[1][1].name][0] == before[source_list[1][1].name][0]
            assert after_repair[source_list[1][1].name][2] == [1, 2]
            assert after_repair[source_list[1][1].name][1].read_bytes() == source_list[1][1].read_bytes()
            assert after_retry == after_repair
    finally:
        asyncio.run(engine.dispose())


def test_repair_handoff_and_retry_execute_exactly_one_database_owned_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CountingEmbedder(FakeEmbedder):
        def __init__(self) -> None:
            self.document_calls = 0

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            self.document_calls += 1
            return await super().embed_documents(texts)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'repair-retry-owner.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def create_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def state(document_id: uuid.UUID) -> tuple[str, uuid.UUID | None, list[tuple[int, str, int]]]:
        async with sessions() as session:
            document = await session.get(Document, document_id)
            jobs = list((await session.scalars(
                select(IngestionJob)
                .where(IngestionJob.document_id == document_id)
                .order_by(IngestionJob.attempt_number)
            )).all())
            assert document is not None and jobs
            return document.status, document.active_ingestion_job_id, [
                (job.attempt_number, job.status, job.attempt_count) for job in jobs
            ]

    asyncio.run(create_schema())
    try:
        with TestClient(app) as client:
            app.state.session_factory = sessions
            app.state.settings = settings(tmp_path)
            app.state.embedder = FakeEmbedder()
            app.state.qdrant = AsyncQdrantClient(location=":memory:", check_compatibility=False)
            app.state.graph_store = GraphStore(str(tmp_path / "repair-retry-graphs"))
            app.state.knowledge_service = KnowledgeService(
                app.state.settings, app.state.qdrant, app.state.embedder, FakeResponder()
            )
            project = client.post("/projects", json={"name": "Repair retry ownership"}).json()
            source = DATASET / "rfis" / "RFI-003_UPS_bypass_clearance.md"
            content = source.read_bytes()
            first = client.post(
                f"/projects/{project['id']}/documents",
                data={"document_type": "RFI"},
                files={"file": (source.name, content, "text/markdown")},
            )
            document_id = uuid.UUID(first.json()["document"]["id"])
            canonical = Path(app.state.settings.upload_dir) / project["id"] / str(document_id) / source.name
            canonical.unlink()
            counter = CountingEmbedder()
            app.state.embedder = counter

            real_run = api_module.run_ingestion
            repair_at_handoff = threading.Event()
            release_repair = threading.Event()
            calls = 0
            call_lock = threading.Lock()

            async def pause_first_handoff(*args, **kwargs):
                nonlocal calls
                with call_lock:
                    calls += 1
                    call_number = calls
                if call_number == 1:
                    repair_at_handoff.set()
                    assert await asyncio.to_thread(release_repair.wait, 5)
                return await real_run(*args, **kwargs)

            monkeypatch.setattr(api_module, "run_ingestion", pause_first_handoff)

            def repair_request():
                return client.post(
                    f"/projects/{project['id']}/documents",
                    data={"document_type": "RFI"},
                    files={"file": (source.name, content, "text/markdown")},
                )

            with ThreadPoolExecutor(max_workers=1) as executor:
                repair_future = executor.submit(repair_request)
                assert repair_at_handoff.wait(timeout=5)
                handoff = asyncio.run(state(document_id))
                repair_job_id = handoff[1]
                assert repair_job_id is not None
                assert handoff == ("queued", repair_job_id, [(1, "completed", 1), (2, "queued", 0)])

                second_repair = repair_request()
                assert second_repair.status_code == 409
                assert second_repair.json()["error"]["code"] == "document_repair_in_progress"
                assert asyncio.run(state(document_id)) == handoff

                async def force_reindex() -> None:
                    async with sessions() as session:
                        await reindex_documents(
                            session,
                            app.state.qdrant,
                            counter,
                            app.state.settings,
                            uuid.UUID(project["id"]),
                            document_id,
                            force=True,
                        )

                with pytest.raises(IngestionError) as reindex_error:
                    asyncio.run(force_reindex())
                assert reindex_error.value.code == "ingestion_not_claimed"
                assert asyncio.run(state(document_id)) == handoff

                retry = client.post(f"/projects/{project['id']}/documents/{document_id}/ingest")
                release_repair.set()
                repair = repair_future.result(timeout=10)

            assert retry.status_code == 200
            assert repair.status_code == 409
            assert repair.json()["error"]["code"] == "ingestion_not_claimed"
            assert counter.document_calls == 1
            assert asyncio.run(state(document_id)) == (
                "completed",
                None,
                [(1, "completed", 1), (2, "completed", 1)],
            )
    finally:
        asyncio.run(engine.dispose())


def test_expired_ingestion_owner_is_never_automatically_reclaimed(
    tmp_path: Path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'expired-owner.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    project_id, document_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    stale_token = uuid.uuid4()

    async def exercise() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Project(id=project_id, name="Expired ownership"))
            session.add(
                Document(
                    id=document_id,
                    project_id=project_id,
                    filename="expired.md",
                    storage_path=str(tmp_path / "expired.md"),
                    document_type="RFI",
                    status="processing",
                    content_sha256="a" * 64,
                    mime_type="text/markdown",
                    size_bytes=1,
                    metadata_json={},
                    active_ingestion_job_id=job_id,
                    ingestion_owner_token=stale_token,
                )
            )
            session.add(
                IngestionJob(
                    id=job_id,
                    project_id=project_id,
                    document_id=document_id,
                    attempt_number=1,
                    attempt_count=1,
                    status="processing",
                    owner_token=stale_token,
                    lease_expires_at=datetime.now(UTC) - timedelta(minutes=1),
                )
            )
            await session.commit()

        async with sessions() as contender:
            document = await contender.get(Document, document_id)
            job = await contender.get(IngestionJob, job_id)
            assert document is not None and job is not None
            with pytest.raises(IngestionError) as error:
                await _claim_ingestion(contender, document, job)
            assert error.value.code == "ingestion_not_claimed"

        async with sessions() as original_owner:
            document = await original_owner.get(Document, document_id)
            job = await original_owner.get(IngestionJob, job_id)
            assert document is not None and job is not None
            assert await _complete_ingestion(
                original_owner, document, job, stale_token, {"page_count": 1}, 1
            ) is True

        async with sessions() as verify:
            document = await verify.get(Document, document_id)
            job = await verify.get(IngestionJob, job_id)
            assert document is not None and job is not None
            assert (document.status, document.page_count, document.ingestion_owner_token) == (
                "completed",
                1,
                None,
            )
            assert (job.status, job.chunk_count, job.attempt_count, job.error) == (
                "completed",
                1,
                1,
                None,
            )

    try:
        asyncio.run(exercise())
    finally:
        asyncio.run(engine.dispose())


def test_retry_vs_retry_and_repair_vs_forced_reindex_are_database_fenced(tmp_path: Path) -> None:
    class CountingEmbedder(FakeEmbedder):
        def __init__(self) -> None:
            self.document_calls = 0

        async def embed_documents(self, texts: list[str]) -> list[list[float]]:
            self.document_calls += 1
            return await super().embed_documents(texts)

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runner-races.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    project_id, document_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    source_path = tmp_path / "retry.md"
    source_path.write_text("# RFI\nUPS clearance is 1200 mm.", encoding="utf-8")
    embedder = CountingEmbedder()
    qdrant = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    task_settings = settings(tmp_path)

    async def exercise() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Project(id=project_id, name="Runner races"))
            session.add(
                Document(
                    id=document_id,
                    project_id=project_id,
                    filename=source_path.name,
                    storage_path=str(source_path),
                    document_type="RFI",
                    status="failed",
                    content_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
                    mime_type="text/markdown",
                    size_bytes=source_path.stat().st_size,
                    metadata_json={},
                )
            )
            session.add(
                IngestionJob(
                    id=job_id,
                    project_id=project_id,
                    document_id=document_id,
                    attempt_number=1,
                    status="failed",
                )
            )
            await session.commit()

        async def retry_runner() -> IngestionJob:
            async with sessions() as session:
                document = await session.get(Document, document_id)
                job = await session.get(IngestionJob, job_id)
                assert document is not None and job is not None
                return await run_ingestion(session, qdrant, embedder, task_settings, document, job)

        retry_results = await asyncio.gather(retry_runner(), retry_runner(), return_exceptions=True)
        assert sum(isinstance(result, IngestionJob) for result in retry_results) == 1
        loser = next(result for result in retry_results if isinstance(result, IngestionError))
        assert loser.code == "ingestion_not_claimed"
        assert embedder.document_calls == 1

        repair_token = uuid.uuid4()
        repair_job_id = uuid.uuid4()
        async with sessions() as session:
            await session.execute(
                update(Document)
                .where(Document.id == document_id)
                .values(
                    status="processing",
                    active_ingestion_job_id=repair_job_id,
                    ingestion_owner_token=repair_token,
                )
            )
            session.add(
                IngestionJob(
                    id=repair_job_id,
                    project_id=project_id,
                    document_id=document_id,
                    attempt_number=2,
                    attempt_count=1,
                    status="processing",
                    owner_token=repair_token,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=30),
                )
            )
            await session.commit()

        async with sessions() as reindex_session:
            with pytest.raises(IngestionError) as error:
                await reindex_documents(
                    reindex_session,
                    qdrant,
                    embedder,
                    task_settings,
                    project_id,
                    document_id,
                    force=True,
                )
            assert error.value.code == "ingestion_not_claimed"

        async with sessions() as verify:
            document = await verify.get(Document, document_id)
            jobs = list(
                (
                    await verify.scalars(
                        select(IngestionJob)
                        .where(IngestionJob.document_id == document_id)
                        .order_by(IngestionJob.attempt_number)
                    )
                ).all()
            )
            assert document is not None
            assert (document.status, document.active_ingestion_job_id, document.ingestion_owner_token) == (
                "processing",
                repair_job_id,
                repair_token,
            )
            assert [(job.attempt_number, job.status) for job in jobs] == [(1, "completed"), (2, "processing")]
            assert embedder.document_calls == 1

    try:
        asyncio.run(exercise())
    finally:
        asyncio.run(engine.dispose())
