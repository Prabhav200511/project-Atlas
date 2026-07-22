import re
import uuid
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ingestion import Citation, entity_references
from app.models import (
    CommissioningStep,
    ComplianceFinding,
    Document,
    Equipment,
    EvidenceLink,
    MitigationScenario,
    NonConformance,
    Requirement,
    RFI,
    ScheduleTask as ScheduleTaskRecord,
    Shipment,
    Vendor,
)

if TYPE_CHECKING:
    from app.ingestion import Chunk
    from app.procurement import ProcurementItemInput
    from app.schedule import ScheduleAnalysis


class ThreadModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EquipmentSummary(ThreadModel):
    equipment_id: str
    name: str
    equipment_type: str | None
    status: str
    metadata: dict[str, Any] = Field(validation_alias="metadata_json")


class DocumentThreadItem(ThreadModel):
    id: uuid.UUID
    filename: str
    document_type: str
    status: str
    revision: str | None = None
    approval_status: str | None = None


class FindingThreadItem(ThreadModel):
    id: uuid.UUID
    equipment_id: str
    parameter: str
    required_value: str
    observed_value: str | None
    normalized_unit: str | None
    status: str
    severity: str
    confidence: float
    review_status: str
    reviewer_note: str | None
    specification_citation: dict
    submittal_citation: dict | None


class DigitalThreadResponse(BaseModel):
    project_id: uuid.UUID
    equipment: EquipmentSummary
    current_specification: DocumentThreadItem | None
    current_submittal: DocumentThreadItem | None
    requirements: list[dict[str, Any]]
    compliance_findings: list[FindingThreadItem]
    rfis: list[dict[str, Any]]
    vendor: list[dict[str, Any]]
    shipments: list[dict[str, Any]]
    schedule_tasks: list[dict[str, Any]]
    commissioning_status: list[dict[str, Any]]
    open_ncrs: list[dict[str, Any]]
    mitigation_scenarios: list[dict[str, Any]]
    evidence_links: list[dict[str, Any]]


async def sync_document_entities(
    session: AsyncSession, document: Document, chunks: list["Chunk"], metadata: dict[str, object]
) -> None:
    tags = [str(value) for value in metadata.get("equipment_tags", []) if value]
    if not tags:
        return
    document.equipment_id = tags[0]
    revision = _value(metadata, "revision")
    approval = _value(metadata, "approval_status", "revision_status", "rfi_status")
    for tag in tags:
        equipment = await session.scalar(
            select(Equipment).where(Equipment.project_id == document.project_id, Equipment.equipment_id == tag)
        )
        if not equipment:
            equipment = Equipment(
                project_id=document.project_id,
                equipment_id=tag,
                name=tag,
                equipment_type=tag.split("-", 1)[0],
                metadata_json={},
            )
            session.add(equipment)
        vendor_name = _value(metadata, "vendor")
        if vendor_name and not await session.scalar(
            select(Vendor).where(
                Vendor.project_id == document.project_id,
                Vendor.equipment_id == tag,
                Vendor.name == vendor_name,
            )
        ):
            session.add(
                Vendor(
                    project_id=document.project_id,
                    equipment_id=tag,
                    name=vendor_name,
                    approval_status=approval,
                    metadata_json={"document_id": str(document.id)},
                )
            )
        await _ensure_evidence(session, document, tag, revision, approval)
        if document.document_type == "specification":
            await _sync_requirements(session, document, tag, chunks, metadata, revision, approval)
        elif document.document_type == "RFI":
            await _sync_rfi(session, document, tag, chunks, revision, approval)
        elif document.document_type == "commissioning_record":
            await _sync_commissioning_steps(session, document, tag, chunks, revision, approval)
    if document.document_type == "schedule":
        await _sync_schedule(session, document, chunks, revision, approval)


async def equipment_digital_thread(
    session: AsyncSession, project_id: uuid.UUID, equipment_id: str
) -> DigitalThreadResponse | None:
    equipment = await session.scalar(
        select(Equipment).where(Equipment.project_id == project_id, Equipment.equipment_id == equipment_id)
    )
    if not equipment:
        return None
    links = list((await session.scalars(_scoped(EvidenceLink, project_id, equipment_id))).all())
    document_ids = {item.document_id for item in links}
    documents = list(
        (
            await session.scalars(
                select(Document).where(
                    Document.project_id == project_id,
                    or_(Document.equipment_id == equipment_id, Document.id.in_(document_ids or {uuid.uuid4()})),
                )
            )
        ).all()
    )
    findings = list((await session.scalars(_scoped(ComplianceFinding, project_id, equipment_id))).all())
    requirements = list((await session.scalars(_scoped(Requirement, project_id, equipment_id))).all())
    rfis = list((await session.scalars(_scoped(RFI, project_id, equipment_id))).all())
    vendors = list((await session.scalars(_scoped(Vendor, project_id, equipment_id))).all())
    shipments = list((await session.scalars(_scoped(Shipment, project_id, equipment_id))).all())
    tasks = list((await session.scalars(_scoped(ScheduleTaskRecord, project_id, equipment_id))).all())
    steps = list((await session.scalars(_scoped(CommissioningStep, project_id, equipment_id))).all())
    ncrs = list(
        (
            await session.scalars(
                _scoped(NonConformance, project_id, equipment_id).where(NonConformance.status == "open")
            )
        ).all()
    )
    mitigations = list((await session.scalars(_scoped(MitigationScenario, project_id, equipment_id))).all())
    return DigitalThreadResponse(
        project_id=project_id,
        equipment=EquipmentSummary.model_validate(equipment),
        current_specification=_current_document(documents, "specification"),
        current_submittal=_current_document(documents, "submittal"),
        requirements=[_row(item) for item in requirements],
        compliance_findings=[FindingThreadItem.model_validate(item) for item in findings],
        rfis=[_row(item) for item in rfis],
        vendor=[_row(item) for item in vendors],
        shipments=[_row(item) for item in shipments],
        schedule_tasks=[_row(item) for item in tasks],
        commissioning_status=[_row(item) for item in steps],
        open_ncrs=[_row(item) for item in ncrs],
        mitigation_scenarios=[_row(item) for item in mitigations],
        evidence_links=[_row(item) for item in links],
    )


async def store_procurement_entities(
    session: AsyncSession, project_id: uuid.UUID, items: list["ProcurementItemInput"]
) -> None:
    for item in items:
        equipment = await session.scalar(
            select(Equipment).where(Equipment.project_id == project_id, Equipment.equipment_id == item.equipment_tag)
        )
        if not equipment:
            session.add(
                Equipment(
                    project_id=project_id,
                    equipment_id=item.equipment_tag,
                    name=item.equipment_tag,
                    equipment_type=item.equipment_tag.split("-", 1)[0],
                    metadata_json={},
                )
            )
        vendor = await session.scalar(
            select(Vendor).where(
                Vendor.project_id == project_id,
                Vendor.equipment_id == item.equipment_tag,
                Vendor.name == item.vendor,
            )
        )
        if not vendor:
            vendor = Vendor(project_id=project_id, equipment_id=item.equipment_tag, name=item.vendor, metadata_json={})
            session.add(vendor)
            await session.flush()
        shipment = await session.scalar(
            select(Shipment).where(
                Shipment.project_id == project_id,
                Shipment.equipment_id == item.equipment_tag,
                Shipment.reference == "procurement-dashboard",
            )
        )
        if not shipment:
            shipment = Shipment(
                project_id=project_id,
                equipment_id=item.equipment_tag,
                vendor_id=vendor.id,
                reference="procurement-dashboard",
                status=item.purchase_order_status,
                evidence={"source": "project-supplied input"},
            )
            session.add(shipment)
        shipment.planned_delivery = item.planned_delivery
        shipment.forecast_delivery = item.forecast_delivery
        shipment.status = item.purchase_order_status
    await session.commit()


async def store_mitigation_scenarios(
    session: AsyncSession, document: Document, analysis: "ScheduleAnalysis"
) -> None:
    for risk in analysis.risks:
        tags = entity_references(" ".join([risk.affected_task_name, risk.root_cause, *risk.dependency_chain]))[
            "equipment_tags"
        ]
        for tag in tags:
            existing = await session.scalar(
                select(MitigationScenario).where(
                    MitigationScenario.project_id == document.project_id,
                    MitigationScenario.equipment_id == tag,
                    MitigationScenario.name == f"{risk.affected_task} mitigation",
                )
            )
            if not existing:
                session.add(
                    MitigationScenario(
                        project_id=document.project_id,
                        equipment_id=tag,
                        name=f"{risk.affected_task} mitigation",
                        description="; ".join(risk.mitigation_options),
                        impact={"predicted_delay_days": risk.predicted_delay_days, "severity": risk.severity},
                        evidence=[item.model_dump(mode="json") for item in risk.evidence],
                    )
                )
    await session.commit()


def document_equipment_id(document: Document) -> str | None:
    if document.equipment_id:
        return document.equipment_id
    values = (document.metadata_json or {}).get("equipment_tags", [])
    return str(values[0]) if values else None


def _scoped(model, project_id: uuid.UUID, equipment_id: str):
    return select(model).where(model.project_id == project_id, model.equipment_id == equipment_id)


def _row(item) -> dict[str, Any]:
    return {
        column.name: getattr(item, column.key)
        for column in item.__table__.columns
        if column.name not in {"project_id", "metadata"}
    }


def _current_document(documents: list[Document], document_type: str) -> DocumentThreadItem | None:
    candidates = [item for item in documents if item.document_type == document_type and item.status == "completed"]
    if not candidates:
        return None
    approved = {"approved", "current", "ifc", "issued for bid", "issued for construction", "answered"}
    candidates.sort(
        key=lambda item: (
            str((item.metadata_json or {}).get("approval_status") or (item.metadata_json or {}).get("revision_status") or "").lower() in approved,
            item.created_at,
        ),
        reverse=True,
    )
    item = candidates[0]
    metadata = item.metadata_json or {}
    return DocumentThreadItem(
        id=item.id,
        filename=item.filename,
        document_type=item.document_type,
        status=item.status,
        revision=_value(metadata, "revision"),
        approval_status=_value(metadata, "approval_status", "revision_status", "rfi_status"),
    )


async def _ensure_evidence(session, document, tag, revision, approval):
    existing = await session.scalar(
        select(EvidenceLink).where(
            EvidenceLink.project_id == document.project_id,
            EvidenceLink.equipment_id == tag,
            EvidenceLink.source_type == "Document",
            EvidenceLink.source_id == str(document.id),
            EvidenceLink.document_id == document.id,
        )
    )
    if not existing:
        session.add(
            EvidenceLink(
                project_id=document.project_id,
                equipment_id=tag,
                source_type="Document",
                source_id=str(document.id),
                document_id=document.id,
                relation="documents",
                revision=revision,
                approval_status=approval,
                citation=Citation(document_id=document.id, filename=document.filename, page=1, section="General").model_dump(mode="json"),
            )
        )


async def _sync_requirements(session, document, tag, chunks, metadata, revision, approval):
    for reference in metadata.get("spec_references", []):
        source = next((item for item in chunks if str(reference) in item.text), None)
        if not source or await session.scalar(
            select(Requirement).where(
                Requirement.project_id == document.project_id,
                Requirement.equipment_id == tag,
                Requirement.document_id == document.id,
                Requirement.parameter == str(reference),
            )
        ):
            continue
        session.add(
            Requirement(
                project_id=document.project_id,
                equipment_id=tag,
                document_id=document.id,
                parameter=str(reference),
                required_value=source.text,
                revision=revision,
                approval_status=approval,
                citation=Citation(document_id=document.id, filename=document.filename, page=source.page, section=source.section).model_dump(mode="json"),
            )
        )


async def _sync_rfi(session, document, tag, chunks, revision, approval):
    if await session.scalar(
        select(RFI).where(RFI.project_id == document.project_id, RFI.equipment_id == tag, RFI.document_id == document.id)
    ):
        return
    text = "\n".join(item.text for item in chunks)
    question = re.search(r"\*\*Question:\*\*\s*(.+?)(?:\n\*\*|$)", text, re.DOTALL)
    answer = re.search(r"\*\*Answer:\*\*\s*(.+?)(?:\n\*\*|$)", text, re.DOTALL)
    session.add(
        RFI(
            project_id=document.project_id,
            equipment_id=tag,
            document_id=document.id,
            rfi_number=Path(document.filename).stem.split("_", 1)[0],
            status=approval or "unknown",
            question=(question.group(1).strip() if question else text[:1000]),
            answer=answer.group(1).strip() if answer else None,
            revision=revision,
            approval_status=approval,
            citation=Citation(document_id=document.id, filename=document.filename, page=1, section="General").model_dump(mode="json"),
        )
    )


async def _sync_schedule(session, document, chunks, revision, approval):
    for chunk in chunks:
        values = dict(re.findall(r"^([^:\n]+):\s*(.*)$", chunk.text, re.MULTILINE))
        task_id, name = values.get("task_id"), values.get("task_name")
        if not task_id or not name:
            continue
        for tag in entity_references(name)["equipment_tags"]:
            if not await session.scalar(
                select(Equipment).where(Equipment.project_id == document.project_id, Equipment.equipment_id == tag)
            ):
                session.add(Equipment(project_id=document.project_id, equipment_id=tag, name=tag, equipment_type=tag.split("-", 1)[0], metadata_json={}))
            if not await session.scalar(
                select(ScheduleTaskRecord).where(
                    ScheduleTaskRecord.project_id == document.project_id,
                    ScheduleTaskRecord.equipment_id == tag,
                    ScheduleTaskRecord.task_id == task_id,
                )
            ):
                session.add(
                    ScheduleTaskRecord(
                        project_id=document.project_id,
                        equipment_id=tag,
                        document_id=document.id,
                        task_id=task_id,
                        name=name,
                        status=values.get("status", "unknown"),
                        dependencies=[value for value in values.get("depends_on", "").split("|") if value],
                        planned_finish=_date(values.get("baseline_finish")),
                        forecast_finish=_date(values.get("forecast_finish")),
                        revision=revision,
                        approval_status=approval,
                        citation=Citation(document_id=document.id, filename=document.filename, page=chunk.page, section=chunk.section).model_dump(mode="json"),
                    )
                )
            if values.get("is_delivery_milestone", "").lower() == "true" and not await session.scalar(
                select(Shipment).where(
                    Shipment.project_id == document.project_id,
                    Shipment.equipment_id == tag,
                    Shipment.reference == task_id,
                )
            ):
                session.add(
                    Shipment(
                        project_id=document.project_id,
                        equipment_id=tag,
                        reference=task_id,
                        status=values.get("status", "unknown"),
                        planned_delivery=_date(values.get("baseline_finish")),
                        forecast_delivery=_date(values.get("forecast_finish")),
                        revision=revision,
                        approval_status=approval,
                        evidence={"document_id": str(document.id), "page": chunk.page, "section": chunk.section},
                    )
                )


async def _sync_commissioning_steps(session, document, tag, chunks, revision, approval):
    index = 0
    for chunk in chunks:
        for line in chunk.text.splitlines():
            match = re.match(r"^\s*\d+\.\s+(.+)", line)
            if not match:
                continue
            index += 1
            if await session.scalar(
                select(CommissioningStep).where(
                    CommissioningStep.project_id == document.project_id,
                    CommissioningStep.equipment_id == tag,
                    CommissioningStep.procedure_document_id == document.id,
                    CommissioningStep.step_index == index,
                )
            ):
                continue
            instruction = match.group(1).strip()
            session.add(
                CommissioningStep(
                    project_id=document.project_id,
                    equipment_id=tag,
                    procedure_document_id=document.id,
                    step_index=index,
                    instruction=instruction,
                    prerequisite=[] if "precondition" in chunk.section.lower() else ["All procedure preconditions are complete"],
                    acceptance_criterion=instruction,
                    evidence=[f"{document.filename}, page {chunk.page}, {chunk.section}"],
                    status="READY" if "precondition" in chunk.section.lower() else "BLOCKED",
                    revision=revision,
                    approval_status=approval,
                    citation=Citation(document_id=document.id, filename=document.filename, page=chunk.page, section=chunk.section).model_dump(mode="json"),
                )
            )


def _value(metadata: dict[str, object], *keys: str) -> str | None:
    return next((str(metadata[key]) for key in keys if metadata.get(key)), None)


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None
