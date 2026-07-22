import csv
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field

from app.config import Settings
from app.ingestion import Citation, IngestionError
from app.llm import GeminiGateway
from app.models import Document


@dataclass
class ScheduleTask:
    task_id: str
    name: str
    dependencies: list[str]
    category: str
    is_delivery_milestone: bool
    baseline_start: date
    baseline_finish: date
    forecast_start: date
    forecast_finish: date
    procurement_status: str
    reported_delay_days: int
    equipment_id: str | None
    notes: str

    @property
    def baseline_duration(self) -> int:
        return (self.baseline_finish - self.baseline_start).days

    @property
    def forecast_duration(self) -> int:
        return (self.forecast_finish - self.forecast_start).days


class ProcurementInput(BaseModel):
    status: str = "on_track"
    delay_days: int = Field(default=0, ge=0)
    lead_time_days: int | None = Field(default=None, ge=0)


class ScheduleScenario(BaseModel):
    analysis_date: date | None = None
    procurement: dict[str, ProcurementInput] = Field(default_factory=dict)
    equipment_delivery_dates: dict[str, date] = Field(default_factory=dict)
    workforce_availability: float = Field(default=1, gt=0, le=1)
    weather_impact_days: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    mitigation_recovery_days: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)


class ScheduleTaskTiming(BaseModel):
    task_id: str
    task_name: str
    equipment_id: str | None
    earliest_start: date
    earliest_finish: date
    latest_start: date
    latest_finish: date
    total_float_days: int
    critical: bool
    forecast_start: date
    forecast_finish: date
    predicted_delay_days: int


class ScheduleSnapshot(BaseModel):
    snapshot_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    analysis_date: date
    baseline_completion_date: date
    affected_completion_date: date
    critical_path: list[str]
    tasks: list[ScheduleTaskTiming]


class ScheduleRisk(BaseModel):
    analysis_type: str = "scenario-based risk analysis"
    affected_task: str
    affected_task_name: str
    affected_equipment: list[str]
    root_cause: str
    dependency_chain: list[str]
    predicted_delay_days: int
    actual_or_simulated_delay_days: int
    delay_comparison_basis: str
    delay_variance_days: int
    available_float_days: int
    risk_lead_time_days: int
    lead_time_days: int
    first_alert_date: date
    affected_completion_date: date
    severity: str
    mitigation_inputs: dict[str, object]
    mitigation_options: list[str]
    explanation: str
    assumptions: list[str]
    evidence: list[Citation]


class ScheduleAnalysis(BaseModel):
    risks: list[ScheduleRisk]
    assumptions: list[str]
    snapshot: ScheduleSnapshot


class ScheduleNarrator:
    def __init__(self, settings: Settings) -> None:
        self.gateway = GeminiGateway(settings)

    async def enrich(self, risk: ScheduleRisk) -> ScheduleRisk:
        if not getattr(self.gateway, "is_available", bool(getattr(self.gateway, "client", None))):
            return risk
        payload = risk.model_dump(mode="json", exclude={"explanation", "mitigation_options", "assumptions", "evidence"})
        try:
            response = await self.gateway.generate(
                "Explain this deterministic scenario-based schedule risk. From the supplied facts only, return JSON with explanation (string), mitigation_options (string list), and assumptions (string list). Do not call it a trained or historical prediction.",
                str(payload),
                json_output=True,
            )
        except IngestionError:
            return risk
        try:
            narrative = json.loads(response or "{}")
        except json.JSONDecodeError:
            return risk
        updates = {"explanation": narrative.get("explanation", risk.explanation)}
        for field in ("mitigation_options", "assumptions"):
            if isinstance(narrative.get(field), list) and all(isinstance(item, str) for item in narrative[field]):
                updates[field] = narrative[field]
        return risk.model_copy(update=updates)


class ScheduleService:
    def __init__(self, settings: Settings, narrator: ScheduleNarrator | None = None) -> None:
        self.settings = settings
        self.narrator = narrator or ScheduleNarrator(settings)

    async def analyze(self, document: Document, scenario: ScheduleScenario) -> ScheduleAnalysis:
        if document.document_type != "schedule":
            raise IngestionError("invalid_schedule_document", "Select a schedule document")
        tasks = load_schedule(Path(document.storage_path))
        order = validate_dependencies(tasks)
        timings = calculate_cpm(tasks, order)
        forecast_start, forecast_finish, parent, origins = propagate_delays(tasks, order, scenario)
        analysis_date = scenario.analysis_date or date.today()
        risks = []
        for task_id in order:
            task = tasks[task_id]
            delay = (forecast_finish[task_id] - task.baseline_finish).days
            if delay <= 0:
                continue
            chain_ids = dependency_chain(task_id, parent, origins)
            root_id = origins.get(task_id, task_id)
            root = tasks[root_id]
            available_float = timings[task_id].total_float_days
            severity = classify_risk(delay, available_float)
            reported_delay = task.reported_delay_days
            risk = ScheduleRisk(
                affected_task=task_id,
                affected_task_name=task.name,
                affected_equipment=sorted(
                    {tasks[item].equipment_id for item in chain_ids if tasks[item].equipment_id}
                ),
                root_cause=root_cause(root, scenario),
                dependency_chain=[f"{item}: {tasks[item].name}" for item in chain_ids],
                predicted_delay_days=delay,
                actual_or_simulated_delay_days=reported_delay,
                delay_comparison_basis="actual" if task.procurement_status == "complete" else "simulated",
                delay_variance_days=delay - reported_delay,
                available_float_days=available_float,
                risk_lead_time_days=max(0, (task.baseline_finish - analysis_date).days),
                lead_time_days=max(0, (task.baseline_finish - analysis_date).days),
                first_alert_date=analysis_date,
                affected_completion_date=forecast_finish[task_id],
                severity=severity,
                mitigation_inputs=mitigation_inputs(root, scenario),
                mitigation_options=mitigations(root, scenario),
                explanation=f"Scenario-based analysis shows {delay} calendar days of delay propagated through the listed dependency chain.",
                assumptions=assumptions(scenario),
                evidence=[
                    citation(document, root_id),
                    citation(document, task_id),
                ],
            )
            risks.append(await self.narrator.enrich(risk))
        snapshot = ScheduleSnapshot(
            analysis_date=analysis_date,
            baseline_completion_date=max(task.baseline_finish for task in tasks.values()),
            affected_completion_date=max(forecast_finish.values()),
            critical_path=[task_id for task_id in order if timings[task_id].total_float_days == 0],
            tasks=[
                ScheduleTaskTiming(
                    task_id=task_id,
                    task_name=tasks[task_id].name,
                    equipment_id=tasks[task_id].equipment_id,
                    **timings[task_id].__dict__,
                    forecast_start=forecast_start[task_id],
                    forecast_finish=forecast_finish[task_id],
                    predicted_delay_days=(forecast_finish[task_id] - tasks[task_id].baseline_finish).days,
                )
                for task_id in order
            ],
        )
        return ScheduleAnalysis(risks=risks, assumptions=assumptions(scenario), snapshot=snapshot)


def load_schedule(path: Path) -> dict[str, ScheduleTask]:
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    tasks = {}
    for row in rows:
        try:
            task = ScheduleTask(
                task_id=row["task_id"],
                name=row["task_name"],
                dependencies=[item for item in row.get("depends_on", "").split("|") if item],
                category=row["category"],
                is_delivery_milestone=row.get("is_delivery_milestone", "").lower() == "true",
                baseline_start=date.fromisoformat(row["baseline_start"]),
                baseline_finish=date.fromisoformat(row["baseline_finish"]),
                forecast_start=date.fromisoformat(row["forecast_start"]),
                forecast_finish=date.fromisoformat(row["forecast_finish"]),
                procurement_status=row.get("status", "unknown"),
                reported_delay_days=int(row.get("delay_days") or 0),
                equipment_id=row.get("equipment_id") or equipment_id(f"{row['task_name']} {row.get('notes', '')}"),
                notes=row.get("notes", ""),
            )
        except (KeyError, ValueError) as exc:
            raise IngestionError("invalid_schedule", "Schedule contains invalid task data") from exc
        if not task.task_id or task.task_id in tasks:
            raise IngestionError("invalid_schedule", "Schedule task IDs must be unique")
        tasks[task.task_id] = task
    if not tasks:
        raise IngestionError("invalid_schedule", "Schedule is empty")
    return tasks


def validate_dependencies(tasks: dict[str, ScheduleTask]) -> list[str]:
    errors = [f"{task.task_id} references missing dependency {dependency}" for task in tasks.values() for dependency in task.dependencies if dependency not in tasks]
    if errors:
        raise IngestionError("invalid_dependencies", "; ".join(errors))
    remaining = {task_id: set(task.dependencies) for task_id, task in tasks.items()}
    order = []
    while remaining:
        ready = sorted(task_id for task_id, dependencies in remaining.items() if not dependencies)
        if not ready:
            raise IngestionError("invalid_dependencies", "Schedule dependency graph contains a cycle")
        order.extend(ready)
        for task_id in ready:
            del remaining[task_id]
        completed = set(ready)
        for dependencies in remaining.values():
            dependencies.difference_update(completed)
    return order


@dataclass(frozen=True)
class CpmTiming:
    earliest_start: date
    earliest_finish: date
    latest_start: date
    latest_finish: date
    total_float_days: int
    critical: bool


def calculate_cpm(tasks: dict[str, ScheduleTask], order: list[str]) -> dict[str, CpmTiming]:
    earliest_start, earliest_finish = {}, {}
    successors = {task_id: [] for task_id in tasks}
    for task in tasks.values():
        for dependency in task.dependencies:
            successors[dependency].append(task.task_id)
    for task_id in order:
        task = tasks[task_id]
        start = max([task.baseline_start, *(earliest_finish[item] for item in task.dependencies)])
        earliest_start[task_id], earliest_finish[task_id] = start, start + timedelta(days=task.baseline_duration)
    project_finish = max(earliest_finish.values())
    latest_start, latest_finish = {}, {}
    for task_id in reversed(order):
        task = tasks[task_id]
        finish = min([project_finish, *(latest_start[item] for item in successors[task_id])])
        latest_finish[task_id], latest_start[task_id] = finish, finish - timedelta(days=task.baseline_duration)
    return {
        task_id: CpmTiming(
            earliest_start[task_id],
            earliest_finish[task_id],
            latest_start[task_id],
            latest_finish[task_id],
            (latest_start[task_id] - earliest_start[task_id]).days,
            latest_start[task_id] == earliest_start[task_id],
        )
        for task_id in tasks
    }


def total_float(tasks: dict[str, ScheduleTask], order: list[str]) -> dict[str, int]:
    return {task_id: timing.total_float_days for task_id, timing in calculate_cpm(tasks, order).items()}


def propagate_delays(
    tasks: dict[str, ScheduleTask], order: list[str], scenario: ScheduleScenario
) -> tuple[dict[str, date], dict[str, date], dict[str, str | None], dict[str, str]]:
    start_dates, finish, parent, origins = {}, {}, {}, {}
    for task_id in order:
        task = tasks[task_id]
        scenario_delay, direct_cause = scenario_effect(task, scenario)
        duration = max(task.baseline_duration, task.forecast_duration + scenario_delay)
        predecessor = max(task.dependencies, key=lambda item: finish[item], default=None)
        if predecessor:
            baseline_predecessor_finish = max(tasks[item].baseline_finish for item in task.dependencies)
            original_forecast_predecessor_finish = max(tasks[item].forecast_finish for item in task.dependencies)
            lag = max(0, (task.baseline_start - baseline_predecessor_finish).days)
            embedded_ready = original_forecast_predecessor_finish + timedelta(days=lag)
            independent_delay = max(0, (task.forecast_start - embedded_ready).days)
            start = max(
                task.baseline_start,
                finish[predecessor] + timedelta(days=lag + independent_delay),
            )
        else:
            start = task.forecast_start
        delivery_date = scenario.equipment_delivery_dates.get(task_id)
        if task.is_delivery_milestone and delivery_date and delivery_date > start:
            start, direct_cause = delivery_date, f"equipment delivery date constrained to {delivery_date.isoformat()}"
        start_dates[task_id] = start
        finish[task_id] = start + timedelta(days=duration)
        parent[task_id] = predecessor
        if direct_cause:
            origins[task_id] = task_id
        elif predecessor and predecessor in origins:
            origins[task_id] = origins[predecessor]
        elif task.forecast_finish > task.baseline_finish:
            origins[task_id] = task_id
        else:
            origins[task_id] = task_id
    return start_dates, finish, parent, origins


def scenario_effect(task: ScheduleTask, scenario: ScheduleScenario) -> tuple[int, str | None]:
    procurement = scenario.procurement.get(task.task_id)
    extra, causes = 0, []
    if procurement:
        extra += procurement.delay_days
        if procurement.status.lower() not in {"on_track", "complete"}:
            causes.append(f"procurement status is {procurement.status}")
        if procurement.lead_time_days and procurement.lead_time_days > task.forecast_duration:
            lead_time_delay = procurement.lead_time_days - task.forecast_duration
            extra += lead_time_delay
            causes.append(f"procurement lead time adds {lead_time_delay} days")
    if scenario.workforce_availability < 1 and task.category in {"Construction", "Commissioning"}:
        extra += math.ceil(task.forecast_duration * (1 / scenario.workforce_availability - 1))
        causes.append(f"workforce availability is {scenario.workforce_availability:.0%}")
    weather_days = scenario.weather_impact_days.get(task.task_id, 0)
    if weather_days:
        extra += weather_days
        causes.append(f"synthetic weather impact adds {weather_days} days")
    recovery_days = scenario.mitigation_recovery_days.get(task.task_id, 0)
    if recovery_days:
        extra -= recovery_days
        causes.append(f"mitigation recovery removes {recovery_days} days")
    if "shortage" in task.notes.lower():
        causes.insert(0, task.notes)
    return extra, "; ".join(causes) if causes else None


def dependency_chain(task_id: str, parent: dict[str, str | None], origins: dict[str, str]) -> list[str]:
    origin, chain, current = origins[task_id], [], task_id
    while current:
        chain.append(current)
        if current == origin:
            break
        current = parent[current]
    return list(reversed(chain))


def root_cause(task: ScheduleTask, scenario: ScheduleScenario) -> str:
    delivery_date = scenario.equipment_delivery_dates.get(task.task_id)
    if task.is_delivery_milestone and delivery_date:
        return f"equipment delivery date constrained to {delivery_date.isoformat()}"
    _, cause = scenario_effect(task, scenario)
    return cause or "forecast schedule deviation propagated through dependencies"


def classify_risk(delay_days: int, available_float_days: int) -> str:
    if delay_days > available_float_days:
        return "critical"
    if available_float_days <= 7:
        return "high"
    return "medium"


def mitigations(root: ScheduleTask, scenario: ScheduleScenario) -> list[str]:
    options = ["Confirm the recovery schedule and protect successor start dates."]
    if root.category in {"Procurement", "Delivery"} or root.is_delivery_milestone:
        options.append("Expedite the supplier recovery plan and confirm delivery logistics.")
    if scenario.workforce_availability < 1:
        options.append("Re-sequence or add qualified crews to restore planned productivity.")
    if scenario.weather_impact_days:
        options.append("Use weather contingency windows and resequence weather-sensitive work.")
    return options


def mitigation_inputs(root: ScheduleTask, scenario: ScheduleScenario) -> dict[str, object]:
    procurement = scenario.procurement.get(root.task_id)
    return {
        "root_task_id": root.task_id,
        "procurement_delay_days": procurement.delay_days if procurement else 0,
        "delivery_date": scenario.equipment_delivery_dates.get(root.task_id),
        "workforce_availability": scenario.workforce_availability,
        "weather_impact_days": scenario.weather_impact_days.get(root.task_id, 0),
        "recovery_days": scenario.mitigation_recovery_days.get(root.task_id, 0),
    }


def assumptions(scenario: ScheduleScenario) -> list[str]:
    return [
        "This is scenario-based risk analysis, not a trained historical prediction.",
        "Durations and dependency propagation use calendar days from the supplied schedule.",
        f"Workforce availability is modeled at {scenario.workforce_availability:.0%}.",
        f"Synthetic weather impacts are applied only to listed task IDs ({len(scenario.weather_impact_days)} inputs).",
        f"Mitigation recovery is applied only to listed task IDs ({len(scenario.mitigation_recovery_days)} inputs).",
    ]


def citation(document: Document, task_id: str) -> Citation:
    return Citation(document_id=document.id, filename=document.filename, page=1, section=f"Task {task_id}")


def equipment_id(text: str) -> str | None:
    match = re.search(r"\b(?:UPS-[A-Z][A-Z0-9]*|CRAC-\d+|SWGR-[A-Z][A-Z0-9]*)\b", text, re.IGNORECASE)
    return match.group(0).upper() if match else None
