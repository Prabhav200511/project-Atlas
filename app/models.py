import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, synonym


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Equipment(Base):
    __tablename__ = "equipment"
    __table_args__ = (UniqueConstraint("project_id", "equipment_id", name="uq_equipment_project_tag"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(255))
    equipment_type: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50), default="active")
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("project_id", "email", name="uq_users_project_email"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(50), default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("project_id", "content_sha256", name="uq_documents_project_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str | None] = mapped_column(String(100), index=True)
    filename: Mapped[str] = mapped_column(String(512))
    storage_path: Mapped[str] = mapped_column(String(1024))
    document_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50), default="pending")
    content_sha256: Mapped[str] = mapped_column(String(64))
    mime_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column()
    page_count: Mapped[int | None] = mapped_column()
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    active_ingestion_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    ingestion_owner_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        UniqueConstraint("document_id", "attempt_number", name="uq_ingestion_jobs_document_attempt"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(default=1, server_default="1")
    owner_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(50), default="queued")
    chunk_count: Mapped[int] = mapped_column(default=0)
    attempt_count: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ComplianceFinding(Base):
    __tablename__ = "compliance_findings"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "specification_document_id", "submittal_document_id", "requirement_key",
            name="uq_compliance_finding_requirement",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str | None] = mapped_column(String(100), index=True)
    specification_document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    submittal_document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    requirement_key: Mapped[str] = mapped_column(String(100))
    parameter = synonym("requirement_key")
    requirement: Mapped[str] = mapped_column(Text)
    required_value: Mapped[str] = mapped_column(String(255))
    observed_value: Mapped[str | None] = mapped_column(String(255))
    normalized_unit: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30))
    severity: Mapped[str] = mapped_column(String(30))
    explanation: Mapped[str] = mapped_column(Text)
    original_requirement_text: Mapped[str] = mapped_column(Text)
    original_observed_text: Mapped[str | None] = mapped_column(Text)
    specification_citation: Mapped[dict] = mapped_column(JSON)
    submittal_citation: Mapped[dict | None] = mapped_column(JSON)
    confidence: Mapped[float] = mapped_column()
    review_status: Mapped[str] = mapped_column(String(20), default="pending")
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    specification_revision: Mapped[str | None] = mapped_column(String(50))
    specification_approval_status: Mapped[str | None] = mapped_column(String(50))
    submittal_revision: Mapped[str | None] = mapped_column(String(50))
    submittal_approval_status: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CommissioningTestRecord(Base):
    __tablename__ = "commissioning_test_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str | None] = mapped_column(String(100), index=True)
    procedure_document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    completed_steps: Mapped[int] = mapped_column(default=0)
    total_steps: Mapped[int] = mapped_column(default=0)
    coverage_percent: Mapped[float] = mapped_column(default=0)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NonConformance(Base):
    __tablename__ = "non_conformances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str | None] = mapped_column(String(100), index=True)
    test_record_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("commissioning_test_records.id"), index=True)
    procedure_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id"), index=True)
    compliance_finding_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("compliance_findings.id"), index=True)
    step_index: Mapped[int] = mapped_column()
    criterion: Mapped[str] = mapped_column(Text)
    observation: Mapped[str] = mapped_column(Text)
    citation: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Requirement(Base):
    __tablename__ = "requirements"
    __table_args__ = (UniqueConstraint("project_id", "equipment_id", "document_id", "parameter", name="uq_requirement_source"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str] = mapped_column(String(100), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    parameter: Mapped[str] = mapped_column(String(255))
    required_value: Mapped[str] = mapped_column(Text)
    normalized_unit: Mapped[str | None] = mapped_column(String(50))
    revision: Mapped[str | None] = mapped_column(String(50))
    approval_status: Mapped[str | None] = mapped_column(String(50))
    citation: Mapped[dict] = mapped_column(JSON)


class Vendor(Base):
    __tablename__ = "vendors"
    __table_args__ = (UniqueConstraint("project_id", "equipment_id", "name", name="uq_vendor_equipment"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(255))
    approval_status: Mapped[str | None] = mapped_column(String(50))
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)


class Shipment(Base):
    __tablename__ = "shipments"
    __table_args__ = (UniqueConstraint("project_id", "equipment_id", "reference", name="uq_shipment_reference"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str] = mapped_column(String(100), index=True)
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("vendors.id"), index=True)
    reference: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50))
    planned_delivery: Mapped[date | None] = mapped_column(Date)
    forecast_delivery: Mapped[date | None] = mapped_column(Date)
    required_on_site_date: Mapped[date | None] = mapped_column(Date)
    location: Mapped[str | None] = mapped_column(String(255))
    available_float_days: Mapped[int] = mapped_column(default=0)
    schedule_task_id: Mapped[str | None] = mapped_column(String(100), index=True)
    first_alert_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[str | None] = mapped_column(String(50))
    approval_status: Mapped[str | None] = mapped_column(String(50))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)


class ShipmentEvent(Base):
    __tablename__ = "shipment_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    shipment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shipments.id"), index=True)
    equipment_id: Mapped[str] = mapped_column(String(100), index=True)
    event_type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50))
    location: Mapped[str | None] = mapped_column(String(255))
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


class ScheduleTask(Base):
    __tablename__ = "schedule_tasks"
    __table_args__ = (UniqueConstraint("project_id", "equipment_id", "task_id", name="uq_schedule_task_equipment"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str] = mapped_column(String(100), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    task_id: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(50))
    dependencies: Mapped[list] = mapped_column(JSON, default=list)
    planned_finish: Mapped[date | None] = mapped_column(Date)
    forecast_finish: Mapped[date | None] = mapped_column(Date)
    available_float_days: Mapped[int] = mapped_column(default=0)
    revision: Mapped[str | None] = mapped_column(String(50))
    approval_status: Mapped[str | None] = mapped_column(String(50))
    citation: Mapped[dict] = mapped_column(JSON)


class CommissioningStep(Base):
    __tablename__ = "commissioning_steps"
    __table_args__ = (UniqueConstraint("project_id", "equipment_id", "procedure_document_id", "step_index", name="uq_commissioning_step"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str] = mapped_column(String(100), index=True)
    procedure_document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    test_record_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("commissioning_test_records.id"), index=True)
    step_index: Mapped[int] = mapped_column()
    instruction: Mapped[str] = mapped_column(Text)
    prerequisite: Mapped[list] = mapped_column(JSON, default=list)
    acceptance_criterion: Mapped[str] = mapped_column(Text)
    observation: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default="NOT_STARTED")
    reviewer_note: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[str | None] = mapped_column(String(50))
    approval_status: Mapped[str | None] = mapped_column(String(50))
    citation: Mapped[dict] = mapped_column(JSON)


class RFI(Base):
    __tablename__ = "rfis"
    __table_args__ = (UniqueConstraint("project_id", "equipment_id", "document_id", name="uq_rfi_equipment_document"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str] = mapped_column(String(100), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    rfi_number: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(50))
    question: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[str | None] = mapped_column(String(50))
    approval_status: Mapped[str | None] = mapped_column(String(50))
    citation: Mapped[dict] = mapped_column(JSON)


class MitigationScenario(Base):
    __tablename__ = "mitigation_scenarios"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    simulation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(255))
    scenario_key: Mapped[str | None] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50), default="proposed")
    description: Mapped[str] = mapped_column(Text)
    impact: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[str | None] = mapped_column(String(50))
    approval_status: Mapped[str | None] = mapped_column(String(50))
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvidenceLink(Base):
    __tablename__ = "evidence_links"
    __table_args__ = (UniqueConstraint("project_id", "equipment_id", "source_type", "source_id", "document_id", name="uq_evidence_link"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str] = mapped_column(String(100), index=True)
    source_type: Mapped[str] = mapped_column(String(50))
    source_id: Mapped[str] = mapped_column(String(100))
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    relation: Mapped[str] = mapped_column(String(100))
    revision: Mapped[str | None] = mapped_column(String(50))
    approval_status: Mapped[str | None] = mapped_column(String(50))
    citation: Mapped[dict] = mapped_column(JSON)


class ImpactEvent(Base):
    __tablename__ = "impact_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str] = mapped_column(String(100), index=True)
    type: Mapped[str] = mapped_column(String(50), index=True)
    source_id: Mapped[str] = mapped_column(String(255), index=True)
    severity: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float] = mapped_column()
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    assumptions: Mapped[dict] = mapped_column(JSON, default=dict)


class ImpactEdge(Base):
    __tablename__ = "impact_edges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str] = mapped_column(String(100), index=True)
    source_event: Mapped[uuid.UUID] = mapped_column(ForeignKey("impact_events.id"), index=True)
    target_event: Mapped[uuid.UUID] = mapped_column(ForeignKey("impact_events.id"), index=True)
    relationship: Mapped[str] = mapped_column(String(100))
    delay_days: Mapped[int] = mapped_column(default=0)
    confidence: Mapped[float] = mapped_column()


class EvidenceRecord(Base):
    __tablename__ = "evidence_records"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    equipment_id: Mapped[str] = mapped_column(String(100), index=True)
    impact_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("impact_events.id"), index=True)
    claim: Mapped[str] = mapped_column(Text)
    document: Mapped[str] = mapped_column(String(512))
    page: Mapped[int | None] = mapped_column()
    clause: Mapped[str | None] = mapped_column(String(255))
    excerpt: Mapped[str] = mapped_column(Text)
    model_version: Mapped[str] = mapped_column(String(100))
    verification_status: Mapped[str] = mapped_column(String(30))


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    fixture_name: Mapped[str] = mapped_column(String(100))
    fixture_format: Mapped[str] = mapped_column(String(10))
    synthetic_data: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(30), default="RUNNING")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvaluationCase(Base):
    __tablename__ = "evaluation_cases"
    __table_args__ = (UniqueConstraint("evaluation_run_id", "case_key", name="uq_evaluation_run_case"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    evaluation_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_runs.id"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    case_key: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20))
    expected: Mapped[dict] = mapped_column(JSON, default=dict)
    actual: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class WorkflowBenchmark(Base):
    __tablename__ = "workflow_benchmarks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    workflow_type: Mapped[str] = mapped_column(String(60), index=True)
    manual_baseline_seconds: Mapped[float] = mapped_column()
    atlas_execution_seconds: Mapped[float] = mapped_column()
    measurement_source: Mapped[str] = mapped_column(String(255))
    sample_count: Mapped[int] = mapped_column()
    measurement_kind: Mapped[str] = mapped_column(String(20), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    synthetic_data: Mapped[bool] = mapped_column(default=False)
