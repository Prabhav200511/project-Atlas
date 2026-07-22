import mimetypes
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.commissioning import (
    CommissioningProcedure,
    EquipmentReadiness,
    EngineerObservation,
    TestRecordResponse,
    stored_test_record,
)
from app.benchmarks import (
    BenchmarkCreate,
    BenchmarkResponse,
    BenchmarkSummary,
    record_benchmark,
    summarize_benchmarks,
)
from app.compliance import ComplianceFindingResponse, ComplianceMetrics, evaluate_ground_truth, finding_response, review_finding
from app.context import ContextBundle
from app.equipment import DigitalThreadResponse, equipment_digital_thread, store_mitigation_scenarios, store_procurement_entities
from app.evaluation import EvaluationRunRequest, EvaluationRunResponse, get_evaluation_run, run_evaluation
from app.demo import VerticalDemoResponse, seed_vertical_demo
from app.executive import ExecutiveSummary, executive_summary
from app.ingestion import DocumentType, IngestionError, RetrievalResult, file_hash, retrieve_chunks, run_ingestion, validate_upload
from app.impact_chain import (
    EquipmentImpactChain,
    ImpactChainResponse,
    ImpactChainStart,
    ImpactDecision,
    ImpactEventCreate,
    equipment_impact_chain,
    propagate_event,
)
from app.models import AuditEvent, ComplianceFinding, Document, Equipment, IngestionJob, Project, ScheduleTask
from app.mitigation import (
    MitigationSelectionRequest,
    MitigationSelectionResponse,
    MitigationSimulationRequest,
    MitigationSimulationResponse,
    select_mitigation,
    simulate_mitigations,
)
from app.procurement import (
    AlternativeComparisonResponse,
    ImportedShipmentAssessment,
    ProcurementDashboard,
    ProcurementItemInput,
    ShipmentListResponse,
    ShipmentImportResponse,
    ShipmentRiskResponse,
    SyntheticRiskEventInput,
    SyntheticRiskEventResponse,
    ShipmentTimelineResponse,
    assess_persisted_shipment,
    compare_alternatives,
    inject_risk_event,
    import_shipment_csv,
    imported_shipment_assessments,
    list_shipments,
    reset_synthetic_supply_chain,
    seed_synthetic_supply_chain,
    shipment_risk,
    shipment_timeline,
)
from app.schedule import ScheduleAnalysis, ScheduleScenario, ScheduleSnapshot
from app.workflow import ConversationMessage, CopilotResult, QueryPlanResult, RfiResult

router = APIRouter(prefix="/projects", tags=["projects"])
evaluation_router = APIRouter(prefix="/api/evaluation", tags=["evaluation"])
mitigation_router = APIRouter(prefix="/api/mitigations", tags=["mitigations"])
benchmark_router = APIRouter(prefix="/api/benchmarks", tags=["benchmarks"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class ProjectResponse(BaseModel):
    id: uuid.UUID
    name: str


class DocumentResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    filename: str
    document_type: str
    status: str
    page_count: int | None
    metadata: dict = Field(default_factory=dict)


class IngestionResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    status: str
    chunk_count: int
    attempt_count: int
    error: str | None


class UploadResponse(BaseModel):
    document: DocumentResponse
    ingestion: IngestionResponse


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=3, max_length=2_000)
    limit: int = Field(default=12, ge=1, le=12)


class RetrievalResponse(BaseModel):
    results: list[RetrievalResult]


class CopilotRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2_000)
    history: list[ConversationMessage] = Field(default_factory=list)


class RfiMatchRequest(BaseModel):
    proposed_rfi: str = Field(min_length=10, max_length=8_000)
    similarity_threshold: float | None = Field(default=None, ge=0, le=1)


class GraphResponse(BaseModel):
    project_id: str
    nodes: list[dict]
    edges: list[dict]


class ComplianceCheckRequest(BaseModel):
    specification_document_id: uuid.UUID
    submittal_document_id: uuid.UUID


class ComplianceCheckResponse(BaseModel):
    findings: list[ComplianceFindingResponse]


class ComplianceReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected|needs_review|needs-review)$")
    reviewer_id: uuid.UUID | None = None
    reviewer_note: str | None = Field(default=None, max_length=2_000)


class ScheduleAnalysisRequest(ScheduleScenario):
    schedule_document_id: uuid.UUID


class CommissioningRecordRequest(BaseModel):
    procedure_document_id: uuid.UUID
    observations: list[EngineerObservation] = Field(default_factory=list)


class ProcurementDashboardRequest(BaseModel):
    items: list[ProcurementItemInput] = Field(default_factory=list, max_length=50)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def _document_response(document: Document) -> DocumentResponse:
    return DocumentResponse(
        id=document.id,
        project_id=document.project_id,
        filename=document.filename,
        document_type=document.document_type,
        status=document.status,
        page_count=document.page_count,
        metadata=document.metadata_json or {},
    )


def _job_response(job: IngestionJob) -> IngestionResponse:
    return IngestionResponse(
        id=job.id,
        document_id=job.document_id,
        status=job.status,
        chunk_count=job.chunk_count,
        attempt_count=job.attempt_count,
        error=job.error,
    )


async def _project_or_404(session: AsyncSession, project_id: uuid.UUID) -> Project:
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


async def _document_or_404(session: AsyncSession, project_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    document = await session.scalar(
        select(Document).where(Document.id == document_id, Document.project_id == project_id)
    )
    if not document:
        raise HTTPException(404, "Document not found")
    return document


async def _latest_job(session: AsyncSession, project_id: uuid.UUID, document_id: uuid.UUID) -> IngestionJob:
    job = await session.scalar(
        select(IngestionJob)
        .where(IngestionJob.project_id == project_id, IngestionJob.document_id == document_id)
        .order_by(IngestionJob.created_at.desc())
    )
    if not job:
        raise HTTPException(404, "Ingestion job not found")
    return job


async def _cleanup_document(session: AsyncSession, qdrant: object, settings: object, doc: Document) -> None:
    from qdrant_client.models import FilterSelector
    from sqlalchemy import delete, update

    from app.models import (
        CommissioningStep,
        EquipmentItem,
        EvidenceLink,
        ProcedureStep,
        RFIAttachment,
        RFILink,
        SpecificationRequirement,
    )
    from app.vector import document_filter

    try:
        await qdrant.delete(
            collection_name=settings.qdrant_collection,
            points_selector=FilterSelector(filter=document_filter(doc.project_id, doc.id)),
            wait=True,
        )
    except Exception:
        pass
    Path(doc.storage_path).unlink(missing_ok=True)
    await session.execute(delete(IngestionJob).where(IngestionJob.document_id == doc.id))
    await session.execute(
        delete(SpecificationRequirement).where(
            (SpecificationRequirement.specification_document_id == doc.id)
            | (SpecificationRequirement.submittal_document_id == doc.id)
        )
    )
    await session.execute(delete(ProcedureStep).where(ProcedureStep.procedure_document_id == doc.id))
    await session.execute(delete(RFILink).where(RFILink.document_id == doc.id))
    await session.execute(
        delete(CommissioningStep).where(
            (CommissioningStep.document_id == doc.id) | (CommissioningStep.procedure_document_id == doc.id)
        )
    )
    await session.execute(delete(RFIAttachment).where(RFIAttachment.document_id == doc.id))
    await session.execute(delete(EvidenceLink).where(EvidenceLink.document_id == doc.id))
    await session.execute(
        update(EquipmentItem).where(EquipmentItem.procedure_document_id == doc.id).values(procedure_document_id=None)
    )
    await session.delete(doc)
    await session.commit()


@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(payload: ProjectCreate, session: AsyncSession = Depends(get_session)) -> ProjectResponse:
    project = Project(name=payload.name)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return ProjectResponse(id=project.id, name=project.name)


@router.get("", response_model=list[ProjectResponse])
async def list_projects(session: AsyncSession = Depends(get_session)) -> list[ProjectResponse]:
    projects = (await session.scalars(select(Project).order_by(Project.created_at.desc()))).all()
    return [ProjectResponse(id=project.id, name=project.name) for project in projects]


@router.get("/{project_id}/documents", response_model=list[DocumentResponse])
async def list_documents(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[DocumentResponse]:
    await _project_or_404(session, project_id)
    documents = (await session.scalars(
        select(Document).where(Document.project_id == project_id).order_by(Document.created_at.desc())
    )).all()
    return [_document_response(document) for document in documents]


@router.post("/{project_id}/documents", response_model=UploadResponse, status_code=201)
async def upload_document(
    project_id: uuid.UUID,
    request: Request,
    document_type: DocumentType = Form(...),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> UploadResponse:
    await _project_or_404(session, project_id)
    content = await file.read()
    filename = Path(file.filename or "").name
    validate_upload(filename, document_type, len(content), request.app.state.settings)
    content_sha256 = file_hash(content)
    existing_docs = (
        await session.scalars(
            select(Document).where(
                Document.project_id == project_id,
                (Document.content_sha256 == content_sha256) | (Document.filename == filename),
            )
        )
    ).all()
    for existing_doc in existing_docs:
        if existing_doc.status != "completed" or not Path(existing_doc.storage_path).exists():
            await _cleanup_document(session, request.app.state.qdrant, request.app.state.settings, existing_doc)
        elif existing_doc.content_sha256 == content_sha256:
            raise HTTPException(409, "An identical document already exists in this project")
    document_id = uuid.uuid4()
    upload_path = Path(request.app.state.settings.upload_dir) / str(project_id) / str(document_id) / filename
    upload_path.parent.mkdir(parents=True, exist_ok=True)
    upload_path.write_bytes(content)
    document = Document(
        id=document_id,
        project_id=project_id,
        filename=filename,
        storage_path=str(upload_path),
        document_type=document_type,
        status="queued",
        content_sha256=content_sha256,
        mime_type=file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream",
        size_bytes=len(content),
        metadata_json={},
    )
    job = IngestionJob(project_id=project_id, document_id=document_id, status="queued")
    session.add_all([document, job])
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        upload_path.unlink(missing_ok=True)
        raise HTTPException(409, "An identical document already exists in this project") from exc
    try:
        await run_ingestion(
            session, request.app.state.qdrant, request.app.state.embedder, request.app.state.settings, document, job, request.app.state.graph_store
        )
    except IngestionError as exc:
        exc.details = {"document_id": str(document.id), "ingestion_job_id": str(job.id), "status": job.status}
        raise
    return UploadResponse(document=_document_response(document), ingestion=_job_response(job))


@router.post("/{project_id}/documents/{document_id}/ingest", response_model=UploadResponse)
async def ingest_document(
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> UploadResponse:
    document = await _document_or_404(session, project_id, document_id)
    job = await _latest_job(session, project_id, document_id)
    if document.status in {"processing", "completed"}:
        raise HTTPException(409, f"Document ingestion is already {document.status}")
    try:
        await run_ingestion(
            session, request.app.state.qdrant, request.app.state.embedder, request.app.state.settings, document, job, request.app.state.graph_store
        )
    except IngestionError as exc:
        exc.details = {"document_id": str(document.id), "ingestion_job_id": str(job.id), "status": job.status}
        raise
    return UploadResponse(document=_document_response(document), ingestion=_job_response(job))


@router.get("/{project_id}/documents/{document_id}/ingestion", response_model=UploadResponse)
async def ingestion_status(
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> UploadResponse:
    document = await _document_or_404(session, project_id, document_id)
    return UploadResponse(
        document=_document_response(document), ingestion=_job_response(await _latest_job(session, project_id, document_id))
    )


@router.post("/{project_id}/retrieve", response_model=RetrievalResponse)
async def retrieve(
    project_id: uuid.UUID,
    payload: RetrievalRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RetrievalResponse:
    await _project_or_404(session, project_id)
    plan = await request.app.state.knowledge_service.query_plan(project_id, payload.query, [])
    results = await retrieve_chunks(
        request.app.state.qdrant,
        request.app.state.embedder,
        request.app.state.settings,
        project_id,
        payload.query,
        payload.limit,
        query_plan=plan,
    )
    return RetrievalResponse(results=results)


@router.post("/{project_id}/context", response_model=ContextBundle)
async def build_context(
    project_id: uuid.UUID,
    payload: RetrievalRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ContextBundle:
    await _project_or_404(session, project_id)
    return await request.app.state.knowledge_service.context_bundle(project_id, payload.query)


@router.post("/{project_id}/copilot", response_model=CopilotResult)
async def project_copilot(
    project_id: uuid.UUID,
    payload: CopilotRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CopilotResult:
    await _project_or_404(session, project_id)
    return await request.app.state.knowledge_service.copilot(project_id, payload.question, payload.history)


@router.post("/{project_id}/query-plan", response_model=QueryPlanResult)
async def query_plan(
    project_id: uuid.UUID,
    payload: CopilotRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> QueryPlanResult:
    await _project_or_404(session, project_id)
    return await request.app.state.knowledge_service.route_query(project_id, payload.question, payload.history)


@router.post("/{project_id}/rfis/matches", response_model=RfiResult)
async def rfi_matches(
    project_id: uuid.UUID,
    payload: RfiMatchRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RfiResult:
    await _project_or_404(session, project_id)
    return await request.app.state.knowledge_service.rfi_matches(
        project_id, payload.proposed_rfi, payload.similarity_threshold
    )


@router.get("/{project_id}/graph", response_model=GraphResponse)
async def project_graph(
    project_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> GraphResponse:
    await _project_or_404(session, project_id)
    return GraphResponse.model_validate(request.app.state.graph_store.export(project_id))


@router.post("/{project_id}/compliance/checks", response_model=ComplianceCheckResponse)
async def run_compliance_check(
    project_id: uuid.UUID,
    payload: ComplianceCheckRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ComplianceCheckResponse:
    specification = await _document_or_404(session, project_id, payload.specification_document_id)
    submittal = await _document_or_404(session, project_id, payload.submittal_document_id)
    findings = await request.app.state.compliance_service.store(session, specification, submittal)
    return ComplianceCheckResponse(findings=[finding_response(finding) for finding in findings])


@router.get("/{project_id}/compliance/findings", response_model=ComplianceCheckResponse)
async def list_compliance_findings(
    project_id: uuid.UUID,
    submittal_document_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
) -> ComplianceCheckResponse:
    query = select(ComplianceFinding).where(ComplianceFinding.project_id == project_id)
    if submittal_document_id:
        query = query.where(ComplianceFinding.submittal_document_id == submittal_document_id)
    findings = (await session.scalars(query.order_by(ComplianceFinding.created_at))).all()
    return ComplianceCheckResponse(findings=[finding_response(finding) for finding in findings])


@router.patch("/{project_id}/compliance/findings/{finding_id}/review", response_model=ComplianceFindingResponse)
async def review_compliance_finding(
    project_id: uuid.UUID,
    finding_id: uuid.UUID,
    payload: ComplianceReviewRequest,
    session: AsyncSession = Depends(get_session),
) -> ComplianceFindingResponse:
    finding = await session.scalar(
        select(ComplianceFinding).where(ComplianceFinding.project_id == project_id, ComplianceFinding.id == finding_id)
    )
    if not finding:
        raise HTTPException(404, "Compliance finding not found")
    reviewed = await review_finding(
        session, finding, payload.decision.replace("-", "_"), payload.reviewer_id, payload.reviewer_note
    )
    return finding_response(reviewed)


@router.get("/{project_id}/compliance/evaluation", response_model=ComplianceMetrics)
async def compliance_evaluation(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ComplianceMetrics:
    findings = (await session.scalars(select(ComplianceFinding).where(ComplianceFinding.project_id == project_id))).all()
    truth_path = Path(__file__).parents[1] / "data" / "synthetic_epc" / "ground_truth.json"
    if not truth_path.exists():
        raise HTTPException(404, "Synthetic ground truth is not available")
    return evaluate_ground_truth(findings, truth_path)


@router.post("/{project_id}/schedule/analysis", response_model=ScheduleAnalysis)
async def analyze_schedule(
    project_id: uuid.UUID,
    payload: ScheduleAnalysisRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ScheduleAnalysis:
    schedule = await _document_or_404(session, project_id, payload.schedule_document_id)
    analysis = await request.app.state.schedule_service.analyze(schedule, payload)
    session.add(
        AuditEvent(
            project_id=project_id,
            event_type="schedule_snapshot_created",
            payload={
                "schedule_document_id": str(schedule.id),
                "snapshot": analysis.snapshot.model_dump(mode="json"),
            },
        )
    )
    for timing in analysis.snapshot.tasks:
        stored_task = await session.scalar(
            select(ScheduleTask).where(
                ScheduleTask.project_id == project_id,
                ScheduleTask.document_id == schedule.id,
                ScheduleTask.task_id == timing.task_id,
            )
        )
        if stored_task:
            stored_task.available_float_days = timing.total_float_days
    await store_mitigation_scenarios(session, schedule, analysis)
    return analysis


@router.get("/{project_id}/schedule/snapshots", response_model=list[ScheduleSnapshot])
async def schedule_snapshots(
    project_id: uuid.UUID,
    schedule_document_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[ScheduleSnapshot]:
    await _project_or_404(session, project_id)
    events = (await session.scalars(
        select(AuditEvent)
        .where(AuditEvent.project_id == project_id, AuditEvent.event_type == "schedule_snapshot_created")
        .order_by(AuditEvent.created_at.desc())
    )).all()
    return [
        ScheduleSnapshot.model_validate(event.payload["snapshot"])
        for event in events
        if not schedule_document_id or event.payload.get("schedule_document_id") == str(schedule_document_id)
    ]


@router.get("/{project_id}/commissioning/procedures/{procedure_document_id}", response_model=CommissioningProcedure)
async def commissioning_procedure(
    project_id: uuid.UUID,
    procedure_document_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CommissioningProcedure:
    return request.app.state.commissioning_service.procedure(
        await _document_or_404(session, project_id, procedure_document_id)
    )


@router.post("/{project_id}/commissioning/records", response_model=TestRecordResponse, status_code=201)
async def record_commissioning_test(
    project_id: uuid.UUID,
    payload: CommissioningRecordRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TestRecordResponse:
    procedure = await _document_or_404(session, project_id, payload.procedure_document_id)
    return await request.app.state.commissioning_service.record(session, procedure, payload.observations)


@router.get("/{project_id}/commissioning/records/{record_id}", response_model=TestRecordResponse)
async def commissioning_test_record(
    project_id: uuid.UUID,
    record_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> TestRecordResponse:
    record = await stored_test_record(session, project_id, record_id)
    if not record:
        raise HTTPException(404, "Commissioning test record not found")
    return record


@router.get("/{project_id}/commissioning/readiness/{equipment_id}", response_model=EquipmentReadiness)
async def commissioning_readiness(
    project_id: uuid.UUID,
    equipment_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> EquipmentReadiness:
    await _project_or_404(session, project_id)
    result = await request.app.state.commissioning_service.readiness(session, project_id, equipment_id)
    if not result:
        raise HTTPException(404, "Equipment not found")
    return result


@router.post("/{project_id}/procurement/dashboard", response_model=ProcurementDashboard)
async def procurement_dashboard(
    project_id: uuid.UUID,
    payload: ProcurementDashboardRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ProcurementDashboard:
    await _project_or_404(session, project_id)
    await store_procurement_entities(session, project_id, payload.items)
    return request.app.state.procurement_service.dashboard(payload.items)


@router.post("/{project_id}/supply-chain/seed", response_model=ShipmentListResponse)
async def seed_supply_chain(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ShipmentListResponse:
    await _project_or_404(session, project_id)
    source = Path(__file__).parents[1] / "data" / "synthetic_epc" / "supply_chain" / "shipments.json"
    return await seed_synthetic_supply_chain(session, project_id, source)


@router.post("/{project_id}/supply-chain/import", response_model=ShipmentImportResponse, status_code=201)
async def import_supply_chain_csv(
    project_id: uuid.UUID,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> ShipmentImportResponse:
    await _project_or_404(session, project_id)
    filename = Path(file.filename or "shipments.csv").name
    if Path(filename).suffix.lower() != ".csv":
        raise HTTPException(422, "Shipment import requires a CSV file")
    return await import_shipment_csv(session, project_id, filename, await file.read())


@router.get(
    "/{project_id}/supply-chain/assessments",
    response_model=list[ImportedShipmentAssessment],
)
async def supply_chain_assessments(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[ImportedShipmentAssessment]:
    await _project_or_404(session, project_id)
    return await imported_shipment_assessments(session, project_id)


@router.post(
    "/{project_id}/supply-chain/shipments/{shipment_id}/assessment",
    response_model=ImportedShipmentAssessment,
)
async def assess_supply_chain_shipment(
    project_id: uuid.UUID,
    shipment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ImportedShipmentAssessment:
    await _project_or_404(session, project_id)
    result = await assess_persisted_shipment(session, project_id, shipment_id)
    if not result:
        raise HTTPException(404, "Imported shipment not found")
    return result


@router.get("/{project_id}/supply-chain/alerts", response_model=list[ImportedShipmentAssessment])
async def supply_chain_alerts(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> list[ImportedShipmentAssessment]:
    await _project_or_404(session, project_id)
    return await imported_shipment_assessments(session, project_id, alerts_only=True)


@router.get(
    "/{project_id}/supply-chain/shipments/{shipment_id}/timeline",
    response_model=ShipmentTimelineResponse,
)
async def supply_chain_timeline(
    project_id: uuid.UUID,
    shipment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ShipmentTimelineResponse:
    await _project_or_404(session, project_id)
    result = await shipment_timeline(session, project_id, shipment_id)
    if not result:
        raise HTTPException(404, "Shipment not found")
    return result


@router.post("/{project_id}/demo/reset", response_model=ShipmentListResponse)
async def reset_demo(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ShipmentListResponse:
    await _project_or_404(session, project_id)
    source = Path(__file__).parents[1] / "data" / "synthetic_epc" / "supply_chain" / "shipments.json"
    return await reset_synthetic_supply_chain(session, project_id, source)


@router.post("/{project_id}/demo/vertical-scenario", response_model=VerticalDemoResponse)
async def create_vertical_demo_scenario(
    project_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> VerticalDemoResponse:
    await _project_or_404(session, project_id)
    source = Path(__file__).parents[1] / "data" / "synthetic_epc" / "supply_chain" / "shipments.json"
    return await seed_vertical_demo(
        session,
        project_id,
        request.app.state.compliance_service,
        request.app.state.commissioning_service,
        source,
    )


@router.get("/{project_id}/executive-summary", response_model=ExecutiveSummary)
async def get_executive_summary(
    project_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ExecutiveSummary:
    await _project_or_404(session, project_id)
    return await executive_summary(session, project_id, request.app.state.commissioning_service)


@router.get("/{project_id}/supply-chain/shipments", response_model=ShipmentListResponse)
async def supply_chain_shipments(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ShipmentListResponse:
    await _project_or_404(session, project_id)
    return await list_shipments(session, project_id)


@router.post(
    "/{project_id}/supply-chain/shipments/{shipment_id}/risk-events",
    response_model=SyntheticRiskEventResponse,
    status_code=201,
)
async def add_supply_chain_risk_event(
    project_id: uuid.UUID,
    shipment_id: uuid.UUID,
    payload: SyntheticRiskEventInput,
    session: AsyncSession = Depends(get_session),
) -> SyntheticRiskEventResponse:
    result = await inject_risk_event(session, project_id, shipment_id, payload)
    if not result:
        raise HTTPException(404, "Shipment not found")
    return result


@router.get(
    "/{project_id}/supply-chain/shipments/{shipment_id}/risk",
    response_model=ShipmentRiskResponse,
)
async def get_supply_chain_risk(
    project_id: uuid.UUID,
    shipment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ShipmentRiskResponse:
    result = await shipment_risk(session, project_id, shipment_id)
    if not result:
        raise HTTPException(404, "Shipment not found")
    return result


@router.get(
    "/{project_id}/supply-chain/shipments/{shipment_id}/alternatives",
    response_model=AlternativeComparisonResponse,
)
async def get_supply_chain_alternatives(
    project_id: uuid.UUID,
    shipment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> AlternativeComparisonResponse:
    result = await compare_alternatives(session, project_id, shipment_id)
    if not result:
        raise HTTPException(404, "Shipment not found")
    return result


@router.get("/{project_id}/equipment/{equipment_id}/digital-thread", response_model=DigitalThreadResponse)
async def get_equipment_digital_thread(
    project_id: uuid.UUID,
    equipment_id: str,
    session: AsyncSession = Depends(get_session),
) -> DigitalThreadResponse:
    await _project_or_404(session, project_id)
    result = await equipment_digital_thread(session, project_id, equipment_id)
    if not result:
        raise HTTPException(404, "Equipment not found")
    return result


@router.post(
    "/{project_id}/equipment/{equipment_id}/impact-chain/events",
    response_model=EquipmentImpactChain,
    status_code=201,
)
async def create_equipment_impact_event(
    project_id: uuid.UUID,
    equipment_id: str,
    payload: ImpactEventCreate,
    session: AsyncSession = Depends(get_session),
) -> EquipmentImpactChain:
    await _project_or_404(session, project_id)
    equipment = await session.scalar(
        select(Equipment).where(
            Equipment.project_id == project_id,
            Equipment.equipment_id == equipment_id,
        )
    )
    if not equipment:
        raise HTTPException(404, "Equipment not found")
    return await propagate_event(session, project_id, equipment_id, payload)


@router.get(
    "/{project_id}/equipment/{equipment_id}/impact-chain",
    response_model=EquipmentImpactChain,
)
async def get_equipment_impact_chain(
    project_id: uuid.UUID,
    equipment_id: str,
    session: AsyncSession = Depends(get_session),
) -> EquipmentImpactChain:
    await _project_or_404(session, project_id)
    equipment = await session.scalar(
        select(Equipment).where(
            Equipment.project_id == project_id,
            Equipment.equipment_id == equipment_id,
        )
    )
    if not equipment:
        raise HTTPException(404, "Equipment not found")
    return await equipment_impact_chain(session, project_id, equipment_id)


@router.post("/{project_id}/impact-chains", response_model=ImpactChainResponse, status_code=201)
async def create_impact_chain(
    project_id: uuid.UUID,
    payload: ImpactChainStart,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ImpactChainResponse:
    await _project_or_404(session, project_id)
    return await request.app.state.impact_chain_service.start(session, project_id, payload)


@router.post("/{project_id}/impact-chains/{chain_id}/decision", response_model=ImpactChainResponse)
async def decide_impact_chain(
    project_id: uuid.UUID,
    chain_id: uuid.UUID,
    payload: ImpactDecision,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ImpactChainResponse:
    await _project_or_404(session, project_id)
    result = await request.app.state.impact_chain_service.decide(session, project_id, chain_id, payload)
    if not result:
        raise HTTPException(404, "Impact chain not found")
    return result


@evaluation_router.post("/run", response_model=EvaluationRunResponse, status_code=201)
async def create_evaluation_run(
    payload: EvaluationRunRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> EvaluationRunResponse:
    await _project_or_404(session, payload.project_id)
    return await run_evaluation(
        session,
        payload,
        request.app.state.compliance_service,
        request.app.state.knowledge_service,
        request.app.state.qdrant,
        request.app.state.settings,
    )


@evaluation_router.get("/runs/{run_id}", response_model=EvaluationRunResponse)
async def read_evaluation_run(
    run_id: uuid.UUID,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> EvaluationRunResponse:
    await _project_or_404(session, project_id)
    result = await get_evaluation_run(session, project_id, run_id)
    if not result:
        raise HTTPException(404, "Evaluation run not found")
    return result


@mitigation_router.post("/simulate", response_model=MitigationSimulationResponse, status_code=201)
async def simulate_counterfactual_mitigations(
    payload: MitigationSimulationRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> MitigationSimulationResponse:
    await _project_or_404(session, payload.project_id)
    return await simulate_mitigations(
        session,
        payload,
        request.app.state.commissioning_service,
    )


@mitigation_router.post(
    "/{simulation_id}/select",
    response_model=MitigationSelectionResponse,
)
async def select_counterfactual_mitigation(
    simulation_id: uuid.UUID,
    payload: MitigationSelectionRequest,
    session: AsyncSession = Depends(get_session),
) -> MitigationSelectionResponse:
    await _project_or_404(session, payload.project_id)
    result = await select_mitigation(session, simulation_id, payload)
    if not result:
        raise HTTPException(404, "Mitigation simulation not found")
    return result


@benchmark_router.post("", response_model=BenchmarkResponse, status_code=201)
async def create_workflow_benchmark(
    payload: BenchmarkCreate,
    session: AsyncSession = Depends(get_session),
) -> BenchmarkResponse:
    await _project_or_404(session, payload.project_id)
    return await record_benchmark(session, payload)


@benchmark_router.get("/summary", response_model=BenchmarkSummary)
async def read_workflow_benchmark_summary(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> BenchmarkSummary:
    await _project_or_404(session, project_id)
    return await summarize_benchmarks(session, project_id)
