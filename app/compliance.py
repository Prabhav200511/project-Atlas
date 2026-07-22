import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.equipment import document_equipment_id
from app.ingestion import Citation, IngestionError, _extract_text, _extract_pdf, _extract_schedule, entity_references
from app.llm import GeminiGateway
from app.models import AuditEvent, ComplianceFinding, Document

ComplianceStatus = Literal["COMPLIANT", "NON_COMPLIANT", "MISSING_INFORMATION", "NEEDS_REVIEW"]


@dataclass(frozen=True)
class Rule:
    key: str
    requirement: str
    comparison: Literal["minimum", "exact"]
    severity: str
    specification_pattern: str
    submittal_pattern: str


RULES = (
    Rule("voltage", "UPS nominal voltage", "exact", "high", r"Nominal system rating:.*?(\d+\s*/\s*\d+\s*V)", r"\|\s*Voltage\s*\|\s*([^|\n]+)"),
    Rule("battery_autonomy", "UPS battery autonomy", "minimum", "high", r"not less than\s+(\d+(?:\.\d+)?\s*minutes?)", r"\|\s*Battery\s*\|\s*([^|\n]+)"),
    Rule("sensible_capacity", "CRAC net sensible capacity", "minimum", "high", r"Minimum net sensible cooling capacity:\s*(\d+(?:\.\d+)?\s*kW)", r"\|\s*Net sensible capacity\s*\|\s*([^|\n]+)"),
    Rule("service_clearance", "Front service clearance", "minimum", "medium", r"not less than\s+(\d+(?:\.\d+)?\s*inches?)", r"\|\s*Service clearance\s*\|\s*([^|\n]+)"),
    Rule("interrupting_rating", "Switchgear interrupting rating", "minimum", "high", r"not less than\s+(\d+(?:\.\d+)?\s*kAIC)", r"\|\s*Interrupting rating\s*\|\s*([^|\n]+)"),
    Rule("enclosure_type", "Switchgear enclosure type", "exact", "high", r"enclosure requirement is\s+([^\.\n]+)", r"\|\s*Enclosure\s*\|\s*([^|\n]+)"),
)


@dataclass
class ExtractedRecord:
    key: str
    raw_value: str
    original_text: str
    normalized_value: float | str | None
    normalized_unit: str | None
    citation: Citation
    equipment_id: str | None = None
    parameter: str | None = None
    revision: str | None = None
    approval_status: str | None = None


class FindingDraft(BaseModel):
    equipment_id: str
    parameter: str
    requirement_key: str
    requirement: str
    required_value: str
    observed_value: str | None
    normalized_unit: str | None
    status: ComplianceStatus
    severity: str
    explanation: str
    original_requirement_text: str
    original_observed_text: str | None
    specification_citation: Citation
    submittal_citation: Citation | None
    confidence: float
    specification_revision: str | None = None
    specification_approval_status: str | None = None
    submittal_revision: str | None = None
    submittal_approval_status: str | None = None


class ComplianceFindingResponse(FindingDraft):
    id: uuid.UUID
    review_status: str
    reviewer_note: str | None
    reviewer_id: uuid.UUID | None


class ComplianceMetrics(BaseModel):
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int
    precision: float
    recall: float
    f1: float


class ComplianceExplainer:
    def __init__(self, settings: Settings) -> None:
        self.gateway = GeminiGateway(settings)

    async def explain(self, draft: FindingDraft) -> str:
        if not getattr(self.gateway, "is_available", bool(getattr(self.gateway, "client", None))):
            return draft.explanation
        try:
            return await self.gateway.generate(
                "Explain this compliance result using only the supplied requirement and observed text. Do not add facts.",
                json.dumps(draft.model_dump(mode="json")),
            ) or draft.explanation
        except IngestionError:
            return draft.explanation


class ComplianceService:
    def __init__(self, settings: Settings, explainer: ComplianceExplainer | None = None) -> None:
        self.settings = settings
        self.explainer = explainer or ComplianceExplainer(settings)

    async def assess(self, specification: Document, submittal: Document) -> list[FindingDraft]:
        if specification.document_type != "specification" or submittal.document_type != "submittal":
            raise IngestionError("invalid_compliance_pair", "Select one specification and one submittal")
        if specification.project_id != submittal.project_id:
            raise IngestionError("project_scope_mismatch", "Specification and submittal must belong to the same project")
        equipment_id = _equipment_id(specification, submittal, self.settings)
        requirements = _extract_records(specification, RULES, "specification_pattern", self.settings, equipment_id)
        observed = {
            record.key: record
            for _, record in _extract_records(submittal, RULES, "submittal_pattern", self.settings, equipment_id)
        }
        findings = []
        for rule, requirement in requirements:
            draft = compare_records(rule, requirement, observed.get(rule.key))
            draft.explanation = await self.explainer.explain(draft)
            findings.append(draft)
        return findings

    async def store(self, session: AsyncSession, specification: Document, submittal: Document) -> list[ComplianceFinding]:
        drafts = await self.assess(specification, submittal)
        stored = []
        for draft in drafts:
            existing = await session.scalar(
                select(ComplianceFinding).where(
                    ComplianceFinding.project_id == specification.project_id,
                    ComplianceFinding.specification_document_id == specification.id,
                    ComplianceFinding.submittal_document_id == submittal.id,
                    ComplianceFinding.requirement_key == draft.requirement_key,
                )
            )
            if existing:
                stored.append(existing)
                session.add(
                    AuditEvent(
                        project_id=specification.project_id,
                        event_type="compliance_finding_reused",
                        payload={"finding_id": str(existing.id), "parameter": draft.parameter},
                    )
                )
                continue
            finding = ComplianceFinding(
                project_id=specification.project_id,
                equipment_id=draft.equipment_id,
                specification_document_id=specification.id,
                submittal_document_id=submittal.id,
                **draft.model_dump(
                    mode="json",
                    exclude={"equipment_id", "parameter", "specification_citation", "submittal_citation"},
                ),
                specification_citation=draft.specification_citation.model_dump(mode="json"),
                submittal_citation=draft.submittal_citation.model_dump(mode="json") if draft.submittal_citation else None,
            )
            session.add(finding)
            await session.flush()
            session.add(
                AuditEvent(
                    project_id=specification.project_id,
                    event_type="compliance_finding_created" if draft.status == "NON_COMPLIANT" else "compliance_finding_recorded",
                    payload={
                        "finding_id": str(finding.id),
                        "equipment_id": draft.equipment_id,
                        "parameter": draft.parameter,
                        "status": draft.status,
                        "submittal_document_id": str(submittal.id),
                    },
                )
            )
            stored.append(finding)
        session.add(
            AuditEvent(
                project_id=specification.project_id,
                event_type="compliance_check_completed",
                payload={
                    "specification_document_id": str(specification.id),
                    "submittal_document_id": str(submittal.id),
                    "finding_count": len(stored),
                },
            )
        )
        await session.commit()
        return stored


def compare_records(rule: Rule, requirement: ExtractedRecord, observed: ExtractedRecord | None) -> FindingDraft:
    if not observed:
        status: ComplianceStatus = "MISSING_INFORMATION"
        explanation = "The submittal does not provide a value for this requirement."
        confidence = 0.95
    elif requirement.normalized_value is None or observed.normalized_value is None:
        status, explanation, confidence = "NEEDS_REVIEW", "The extracted value could not be normalized for deterministic comparison.", 0.7
    elif (
        isinstance(requirement.normalized_value, float)
        and isinstance(observed.normalized_value, float)
        and requirement.normalized_unit != observed.normalized_unit
    ):
        status, explanation, confidence = "NEEDS_REVIEW", "The normalized units are incompatible for deterministic comparison.", 0.7
    elif isinstance(requirement.normalized_value, float) and isinstance(observed.normalized_value, float):
        compliant = observed.normalized_value >= requirement.normalized_value if rule.comparison == "minimum" else abs(observed.normalized_value - requirement.normalized_value) < 0.001
        status = "COMPLIANT" if compliant else "NON_COMPLIANT"
        explanation = _numeric_explanation(rule, requirement, observed, status)
        confidence = 0.99
    elif isinstance(requirement.normalized_value, str) and isinstance(observed.normalized_value, str):
        compliant = requirement.normalized_value == observed.normalized_value
        status = "COMPLIANT" if compliant else "NON_COMPLIANT"
        explanation = _text_explanation(requirement, observed, status)
        confidence = 0.98
    else:
        status, explanation, confidence = "NEEDS_REVIEW", "The required and observed values have incompatible data types.", 0.7
    return FindingDraft(
        equipment_id=requirement.equipment_id or (observed.equipment_id if observed else None) or "UNKNOWN",
        parameter=rule.key,
        requirement_key=rule.key,
        requirement=rule.requirement,
        required_value=requirement.raw_value,
        observed_value=observed.raw_value if observed else None,
        normalized_unit=requirement.normalized_unit,
        status=status,
        severity=rule.severity,
        explanation=explanation,
        original_requirement_text=requirement.original_text,
        original_observed_text=observed.original_text if observed else None,
        specification_citation=requirement.citation,
        submittal_citation=observed.citation if observed else None,
        confidence=confidence,
        specification_revision=requirement.revision,
        specification_approval_status=requirement.approval_status,
        submittal_revision=observed.revision if observed else None,
        submittal_approval_status=observed.approval_status if observed else None,
    )


def _extract_records(
    document: Document,
    rules: tuple[Rule, ...],
    attribute: str,
    settings: Settings,
    equipment_id: str,
) -> list[tuple[Rule, ExtractedRecord]]:
    extracted = _document_text(document, settings)
    records = []
    for rule in rules:
        match = re.search(getattr(rule, attribute), extracted, re.IGNORECASE | re.MULTILINE)
        if match:
            raw = match.group(1).strip()
            line_start = extracted.rfind("\n", 0, match.start())
            line_start = 0 if line_start == -1 else line_start + 1
            line_end = extracted.find("\n", match.end())
            line_end = len(extracted) if line_end == -1 else line_end
            full_line = extracted[line_start:line_end].strip()
            records.append((rule, _record(document, extracted, rule.key, raw, full_line, equipment_id)))
    return records


def _document_text(document: Document, settings: Settings) -> str:
    path = Path(document.storage_path)
    if path.suffix.lower() == ".pdf":
        pages = _extract_pdf(path, settings).pages
    elif path.suffix.lower() == ".csv":
        pages = _extract_schedule(path).pages
    else:
        pages = _extract_text(path).pages
    return "\n".join(f"## Page {page.page}\n{page.text}" for page in pages)


def _record(document: Document, text: str, key: str, raw: str, original: str, equipment_id: str) -> ExtractedRecord:
    normalized_value, normalized_unit = normalize_value(raw)
    page, section = _citation_location(text, original)
    return ExtractedRecord(
        key=key,
        raw_value=raw,
        original_text=original,
        normalized_value=normalized_value,
        normalized_unit=normalized_unit,
        citation=Citation(document_id=document.id, filename=document.filename, page=page, section=section),
        equipment_id=equipment_id,
        parameter=key,
        revision=_metadata_value(document, "revision"),
        approval_status=_metadata_value(document, "approval_status", "revision_status"),
    )


def normalize_value(raw: str) -> tuple[float | str | None, str | None]:
    voltage = re.search(r"(\d+)\s*/\s*(\d+)\s*V", raw, re.IGNORECASE)
    if voltage:
        return f"{voltage.group(1)}/{voltage.group(2)}", "V"
    numeric = re.search(r"(\d+(?:\.\d+)?)\s*(kAIC|kW|MW|minutes?|mins?|inches?|inch|in|mm)\b", raw, re.IGNORECASE)
    if numeric:
        value, unit = float(numeric.group(1)), numeric.group(2).lower()
        conversions = {
            "mw": (1_000, "kW"), "kw": (1, "kW"), "mm": (1 / 25.4, "inches"), "in": (1, "inches"),
            "inch": (1, "inches"), "inches": (1, "inches"), "minute": (1, "minutes"), "minutes": (1, "minutes"),
            "mins": (1, "minutes"), "kaic": (1, "kAIC"),
        }
        factor, normalized_unit = conversions[unit]
        return value * factor, normalized_unit
    enclosure = re.search(r"\btype\s*(\d+[a-z]?)\b", raw, re.IGNORECASE)
    if enclosure:
        return f"type{enclosure.group(1).lower()}", None
    normalized = re.sub(r"[^a-z0-9]", "", raw.lower())
    return (normalized, None) if normalized else (None, None)


def _citation_location(text: str, original: str) -> tuple[int, str]:
    page, section = 1, "General"
    for line in text.splitlines():
        page_marker = re.match(r"## Page (\d+)", line.strip())
        if page_marker:
            page = int(page_marker.group(1))
        elif re.match(r"^#{1,6}\s+", line):
            section = line.lstrip("# ").strip()
        if original in line:
            return page, section
    return page, section


def _numeric_explanation(rule: Rule, requirement: ExtractedRecord, observed: ExtractedRecord, status: ComplianceStatus) -> str:
    verb = "meets" if status == "COMPLIANT" else "does not meet"
    return f"Observed {observed.raw_value} {verb} the {rule.comparison} requirement of {requirement.raw_value}."


def _text_explanation(requirement: ExtractedRecord, observed: ExtractedRecord, status: ComplianceStatus) -> str:
    verb = "matches" if status == "COMPLIANT" else "does not match"
    return f"Observed {observed.raw_value} {verb} the required value {requirement.raw_value}."


def finding_response(finding: ComplianceFinding) -> ComplianceFindingResponse:
    return ComplianceFindingResponse(
        id=finding.id,
        equipment_id=finding.equipment_id or "UNKNOWN",
        parameter=finding.requirement_key,
        requirement_key=finding.requirement_key,
        requirement=finding.requirement,
        required_value=finding.required_value,
        observed_value=finding.observed_value,
        normalized_unit=finding.normalized_unit,
        status=finding.status,
        severity=finding.severity,
        explanation=finding.explanation,
        original_requirement_text=finding.original_requirement_text,
        original_observed_text=finding.original_observed_text,
        specification_citation=Citation.model_validate(finding.specification_citation),
        submittal_citation=Citation.model_validate(finding.submittal_citation) if finding.submittal_citation else None,
        confidence=finding.confidence,
        review_status=finding.review_status,
        reviewer_note=finding.reviewer_note,
        reviewer_id=finding.reviewer_id,
        specification_revision=finding.specification_revision,
        specification_approval_status=finding.specification_approval_status,
        submittal_revision=finding.submittal_revision,
        submittal_approval_status=finding.submittal_approval_status,
    )


def evaluate_ground_truth(findings: list[ComplianceFinding | FindingDraft], truth_path: Path) -> ComplianceMetrics:
    truth = json.loads(truth_path.read_text())
    expected = {
        (Path(item["submittal"]).name, _finding_key(item["finding"])) for item in truth["expected_compliance_findings"]
    }
    actual = {
        (_finding_filename(finding), finding.requirement_key)
        for finding in findings
        if finding.status == "NON_COMPLIANT"
    }
    true_positive = len(actual & expected)
    false_positive = len(actual - expected)
    false_negative = len(expected - actual)
    true_negative = len(
        {
            (_finding_filename(finding), finding.requirement_key)
            for finding in findings
            if finding.status != "NON_COMPLIANT"
        }
        - expected
    )
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0
    return ComplianceMetrics(
        true_positive=true_positive,
        false_positive=false_positive,
        false_negative=false_negative,
        true_negative=true_negative,
        precision=precision,
        recall=recall,
        f1=2 * precision * recall / (precision + recall) if precision + recall else 0,
    )


def _finding_filename(finding: ComplianceFinding | FindingDraft) -> str:
    citation = finding.submittal_citation
    return citation.filename if isinstance(citation, Citation) else str(citation["filename"])


def _finding_key(text: str) -> str:
    mappings = {
        "voltage": "voltage", "battery autonomy": "battery_autonomy", "sensible cooling": "sensible_capacity",
        "service clearance": "service_clearance", "interrupting rating": "interrupting_rating", "enclosure": "enclosure_type",
    }
    return next(value for phrase, value in mappings.items() if phrase in text.lower())


async def review_finding(
    session: AsyncSession,
    finding: ComplianceFinding,
    decision: Literal["approved", "rejected", "needs_review"],
    reviewer_id: uuid.UUID | None,
    reviewer_note: str | None = None,
) -> ComplianceFinding:
    finding.review_status, finding.reviewer_id = decision, reviewer_id
    finding.reviewer_note, finding.reviewed_at = reviewer_note, datetime.now(UTC)
    session.add(
        AuditEvent(
            project_id=finding.project_id,
            actor_id=reviewer_id,
            event_type="compliance_finding_reviewed",
            payload={"finding_id": str(finding.id), "decision": decision, "reviewer_note": reviewer_note},
        )
    )
    await session.commit()
    return finding


def _equipment_id(specification: Document, submittal: Document, settings: Settings) -> str:
    explicit = document_equipment_id(specification) or document_equipment_id(submittal)
    if explicit:
        return explicit
    specification_tags = set(entity_references(_document_text(specification, settings))["equipment_tags"])
    submittal_tags = set(entity_references(_document_text(submittal, settings))["equipment_tags"])
    candidates = specification_tags & submittal_tags or specification_tags or submittal_tags
    if not candidates:
        raise IngestionError("missing_equipment_id", "Selected documents do not identify equipment")
    return sorted(candidates)[0]


def _metadata_value(document: Document, *keys: str) -> str | None:
    metadata = document.metadata_json or {}
    return next((str(metadata[key]) for key in keys if metadata.get(key)), None)
