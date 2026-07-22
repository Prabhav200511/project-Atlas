import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.commissioning import CommissioningService, EquipmentReadiness
from app.compliance import ComplianceFindingResponse, ComplianceService, finding_response
from app.equipment import DigitalThreadResponse, equipment_digital_thread
from app.impact_chain import (
    EquipmentImpactChain,
    EvidenceRecordInput,
    ImpactEventCreate,
    PropagationAssumptions,
    equipment_impact_chain,
    propagate_event,
)
from app.ingestion import IngestionError
from app.mitigation import (
    CounterfactualScenario,
    MitigationRules,
    MitigationSimulationRequest,
    MitigationSimulationResponse,
    simulate_mitigations,
)
from app.models import AuditEvent, Document, ImpactEvent, ScheduleTask, Shipment, ShipmentEvent
from app.procurement import ImportedShipmentAssessment, assess_persisted_shipment, seed_synthetic_supply_chain

EQUIPMENT_ID = "SWGR-A"


class VerticalDemoResponse(BaseModel):
    synthetic_data: Literal[True] = True
    equipment_id: str
    compliance_finding: ComplianceFindingResponse
    shipment_risk: ImportedShipmentAssessment
    commissioning_readiness_before: int
    commissioning_readiness_after: int
    commissioning_readiness: EquipmentReadiness
    impact_chain: EquipmentImpactChain
    mitigation: MitigationSimulationResponse
    recommended_mitigation: CounterfactualScenario
    digital_thread: DigitalThreadResponse


async def seed_vertical_demo(
    session: AsyncSession,
    project_id: uuid.UUID,
    compliance: ComplianceService,
    commissioning: CommissioningService,
    shipment_source: Path,
) -> VerticalDemoResponse:
    specification = await _document(session, project_id, "Switchgear_Specification.md")
    submittal = await _document(session, project_id, "SWGR-002_ArcLine_SWGR-A.md")
    if not specification or not submittal:
        raise IngestionError("demo_documents_missing", "Ingest the synthetic switchgear specification and SWGR-002 submittal first", 409)
    await seed_synthetic_supply_chain(session, project_id, shipment_source)
    shipment = await session.scalar(select(Shipment).where(
        Shipment.project_id == project_id,
        Shipment.equipment_id == EQUIPMENT_ID,
        Shipment.reference == "SYN-SHP-001",
    ))
    task = await session.scalar(select(ScheduleTask).where(
        ScheduleTask.project_id == project_id,
        ScheduleTask.equipment_id == EQUIPMENT_ID,
        ScheduleTask.task_id == "T-140",
    ))
    if not shipment or not task:
        raise IngestionError("demo_schedule_missing", "Ingest the synthetic schedule before creating the vertical scenario", 409)

    evidence = shipment.evidence or {}
    shipment.required_on_site_date = shipment.planned_delivery
    shipment.available_float_days = int(evidence.get("schedule_float_days", 0))
    shipment.schedule_task_id = str(evidence.get("schedule_task_id", task.task_id))
    shipment.location = str(evidence.get("origin", "Synthetic vendor facility"))
    task.available_float_days = shipment.available_float_days
    risk_event = (evidence.get("risk_events") or [{}])[0]
    event_at = datetime.fromisoformat(str(risk_event.get("occurred_at", "2026-04-10T08:00:00+00:00")).replace("Z", "+00:00"))
    shipment.first_alert_at = event_at
    if not await session.scalar(select(ShipmentEvent).where(
        ShipmentEvent.project_id == project_id,
        ShipmentEvent.shipment_id == shipment.id,
        ShipmentEvent.event_type == "VENDOR_RESUBMISSION",
    )):
        session.add(ShipmentEvent(
            project_id=project_id,
            shipment_id=shipment.id,
            equipment_id=EQUIPMENT_ID,
            event_type="VENDOR_RESUBMISSION",
            status="forecast_delayed",
            location=shipment.location,
            event_at=event_at,
            details={"reason": "Synthetic rating deviation requires compliant vendor resubmission", "synthetic_data": True},
        ))
    await session.commit()

    prior_seed = await session.scalar(select(AuditEvent).where(
        AuditEvent.project_id == project_id,
        AuditEvent.event_type == "vertical_demo_seeded",
    ))
    baseline_readiness = (
        int(prior_seed.payload["commissioning_readiness_before"])
        if prior_seed and "commissioning_readiness_before" in prior_seed.payload
        else (await commissioning.readiness(session, project_id, EQUIPMENT_ID)).score
    )
    findings = await compliance.store(session, specification, submittal)
    finding = next((item for item in findings if item.requirement_key == "interrupting_rating"), None)
    if not finding or finding.status != "NON_COMPLIANT":
        raise IngestionError("demo_deviation_missing", "The planted switchgear rating deviation was not detected")
    root = await session.scalar(select(ImpactEvent).where(
        ImpactEvent.project_id == project_id,
        ImpactEvent.equipment_id == EQUIPMENT_ID,
        ImpactEvent.type == "SPEC_DEVIATION",
        ImpactEvent.source_id == str(shipment.id),
    ))
    if not root:
        await propagate_event(
            session,
            project_id,
            EQUIPMENT_ID,
            ImpactEventCreate(
                type="SPEC_DEVIATION",
                source_id=str(shipment.id),
                severity="high",
                confidence=finding.confidence,
                timestamp=event_at,
                assumptions=PropagationAssumptions(
                    vendor_resubmission_days=7,
                    delivery_risk_days=35,
                    schedule_impact_days=28,
                    commissioning_impact_days=0,
                ),
                evidence=[
                    EvidenceRecordInput(
                        claim="The offered interrupting rating is below the project specification minimum.",
                        document=finding.submittal_citation["filename"],
                        page=finding.submittal_citation["page"],
                        clause=finding.submittal_citation["section"],
                        excerpt=finding.original_observed_text or finding.observed_value or "",
                        model_version="deterministic-compliance-v1",
                        verification_status="VERIFIED",
                    )
                ],
            ),
        )
    chain = await equipment_impact_chain(session, project_id, EQUIPMENT_ID)
    delivery_event = next(item for item in chain.events if item.type == "DELIVERY_RISK" and item.source_id == str(shipment.id))
    risk = await assess_persisted_shipment(session, project_id, shipment.id)
    readiness = await commissioning.readiness(session, project_id, EQUIPMENT_ID)
    if not risk or not readiness:
        raise IngestionError("demo_state_incomplete", "Shipment risk or commissioning readiness could not be calculated")
    mitigation = await simulate_mitigations(
        session,
        MitigationSimulationRequest(
            project_id=project_id,
            shipment_id=shipment.id,
            impact_event_id=delivery_event.id,
            rules=MitigationRules(expedite_recovery_days=18, resequence_recovery_days=10),
        ),
        commissioning,
    )
    recommended = min(
        (item for item in mitigation.scenarios if item.key != "do_nothing"),
        key=lambda item: (item.critical_path_exposure_days, item.projected_delay_days),
    )
    thread = await equipment_digital_thread(session, project_id, EQUIPMENT_ID)
    if not thread:
        raise IngestionError("demo_thread_missing", "Switchgear digital thread is unavailable")
    if not prior_seed:
        session.add(AuditEvent(
            project_id=project_id,
            event_type="vertical_demo_seeded",
            payload={
                "equipment_id": EQUIPMENT_ID,
                "finding_id": str(finding.id),
                "shipment_id": str(shipment.id),
                "commissioning_readiness_before": baseline_readiness,
                "commissioning_readiness_after": readiness.score,
                "synthetic_data": True,
            },
        ))
        await session.commit()
    return VerticalDemoResponse(
        equipment_id=EQUIPMENT_ID,
        compliance_finding=finding_response(finding),
        shipment_risk=risk,
        commissioning_readiness_before=baseline_readiness,
        commissioning_readiness_after=readiness.score,
        commissioning_readiness=readiness,
        impact_chain=chain,
        mitigation=mitigation,
        recommended_mitigation=recommended,
        digital_thread=thread,
    )


async def _document(session: AsyncSession, project_id: uuid.UUID, filename: str) -> Document | None:
    return await session.scalar(
        select(Document)
        .where(Document.project_id == project_id, Document.filename == filename, Document.status == "completed")
        .order_by(Document.created_at.desc())
        .limit(1)
    )
