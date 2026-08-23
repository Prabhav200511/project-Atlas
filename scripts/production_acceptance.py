"""Run a sanitized acceptance manifest against a Project Atlas deployment."""

from __future__ import annotations

import argparse
import re
from collections.abc import Callable, Mapping
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
import json
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx


CANARY_NAME = re.compile(r"^Atlas Production Canary [A-Za-z0-9][A-Za-z0-9._:-]*(?:-[A-Za-z0-9._:-]+)*$")
SENSITIVE_KEYS = {
    "api_key",
    "completion",
    "content",
    "document_text",
    "message",
    "prompt",
    "raw_error",
    "response_body",
    "secret",
    "source_text",
}


class AcceptanceStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class AcceptanceStep:
    name: str
    status: AcceptanceStatus
    http_status: int | None
    duration_ms: int
    detail: dict[str, Any]


@dataclass(frozen=True)
class AcceptanceReport:
    deployed_sha: str
    api_url: str
    frontend_url: str
    project_name: str
    project_id: str | None
    steps: list[AcceptanceStep | dict[str, Any]]

    def _payload(self) -> dict[str, Any]:
        rows = [asdict(step) if isinstance(step, AcceptanceStep) else dict(step) for step in self.steps]
        safe_rows = sanitize_detail(rows)
        summary = dict(Counter(str(row["status"]) for row in safe_rows))
        return {
            "deployed_sha": self.deployed_sha,
            "verified_at": datetime.now(UTC).isoformat(),
            "api_url": self.api_url,
            "frontend_url": self.frontend_url,
            "project_name": self.project_name,
            "project_id": self.project_id,
            "summary": summary,
            "steps": safe_rows,
        }

    def write(self, output: Path) -> tuple[Path, Path]:
        payload = self._payload()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        markdown = output.with_suffix(".md")
        lines = [
            "# Project Atlas Production Acceptance",
            "",
            f"- Deployed SHA: `{payload['deployed_sha']}`",
            f"- Verified at: `{payload['verified_at']}`",
            f"- API: `{payload['api_url']}`",
            f"- Frontend: `{payload['frontend_url']}`",
            f"- Synthetic project: `{payload['project_name']}` (`{payload['project_id'] or 'not created'}`)",
            "",
            "| Step | Status | HTTP | Duration (ms) | Safe detail |",
            "|---|---:|---:|---:|---|",
        ]
        for row in payload["steps"]:
            detail = json.dumps(row.get("detail", {}), sort_keys=True).replace("|", "\\|")
            lines.append(
                f"| {row['name']} | {row['status']} | {row.get('http_status') or '-'} | "
                f"{row.get('duration_ms', 0)} | `{detail}` |"
            )
        markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return output, markdown


def sanitize_detail(value: Any) -> Any:
    """Return report-safe metadata while structurally excluding sensitive values."""
    if isinstance(value, Mapping):
        return {
            str(key): sanitize_detail(item)
            for key, item in value.items()
            if str(key).lower() not in SENSITIVE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_detail(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class AcceptanceRunner:
    def __init__(
        self,
        *,
        api_url: str,
        frontend_url: str,
        project_name: str,
        allow_synthetic_mutations: bool,
        client: httpx.Client,
    ) -> None:
        if not CANARY_NAME.fullmatch(project_name):
            raise ValueError("project name must identify an Atlas synthetic canary")
        self.api_url = api_url.rstrip("/")
        self.frontend_url = frontend_url.rstrip("/")
        self.project_name = project_name
        self.allow_synthetic_mutations = allow_synthetic_mutations
        self.client = client

    def run_foundation(self) -> tuple[list[AcceptanceStep], str | None]:
        steps: list[AcceptanceStep] = []
        health, _ = self.json_step(
            "liveness",
            "GET",
            "/health",
            validate=lambda body: isinstance(body, dict) and body.get("status") == "ok",
            failure_reason="liveness_contract_failed",
        )
        steps.append(health)
        ready, _ = self.json_step(
            "readiness",
            "GET",
            "/ready",
            validate=lambda body: isinstance(body, dict)
            and body.get("status") == "ok"
            and all((body.get("components") or {}).get(name) == "ok" for name in ("api", "database", "qdrant")),
            failure_reason="readiness_contract_failed",
        )
        steps.append(ready)
        openapi, _ = self.json_step(
            "openapi",
            "GET",
            "/openapi.json",
            validate=lambda body: isinstance(body, dict) and bool(body.get("openapi")) and bool(body.get("paths")),
            failure_reason="openapi_contract_failed",
        )
        steps.append(openapi)
        steps.append(self._cors_step())
        steps.append(self._frontend_identity_step())
        project_step, project_id = self.ensure_project()
        steps.append(project_step)
        return steps, project_id

    def _cors_step(self) -> AcceptanceStep:
        started = perf_counter()
        try:
            response = self.client.options(
                f"{self.api_url}/projects",
                headers={"Origin": self.frontend_url, "Access-Control-Request-Method": "GET"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            return self._network_failure("cors", started, exc)
        allowed = response.headers.get("access-control-allow-origin")
        return AcceptanceStep(
            name="cors",
            status=(
                AcceptanceStatus.PASS
                if response.is_success and allowed == self.frontend_url
                else AcceptanceStatus.FAIL
            ),
            http_status=response.status_code,
            duration_ms=round((perf_counter() - started) * 1000),
            detail={} if allowed == self.frontend_url else {"reason": "cors_origin_mismatch"},
        )

    def _frontend_identity_step(self) -> AcceptanceStep:
        started = perf_counter()
        try:
            response = self.client.get(self.frontend_url, timeout=60)
        except httpx.HTTPError as exc:
            return self._network_failure("frontend_identity", started, exc)
        text = response.text
        accepted = (
            response.is_success
            and "Project Atlas" in text
            and "EPC project intelligence" in text
            and not any(term in text for term in ("Droughts", "Flooding", "Global Warming"))
        )
        return AcceptanceStep(
            name="frontend_identity",
            status=AcceptanceStatus.PASS if accepted else AcceptanceStatus.FAIL,
            http_status=response.status_code,
            duration_ms=round((perf_counter() - started) * 1000),
            detail={} if accepted else {"reason": "frontend_identity_mismatch"},
        )

    @staticmethod
    def _network_failure(name: str, started: float, exc: httpx.HTTPError) -> AcceptanceStep:
        return AcceptanceStep(
            name=name,
            status=AcceptanceStatus.FAIL,
            http_status=None,
            duration_ms=round((perf_counter() - started) * 1000),
            detail={"reason": "network_error", "error_type": type(exc).__name__},
        )

    def ensure_project(self) -> tuple[AcceptanceStep, str | None]:
        if not self.allow_synthetic_mutations:
            return (
                AcceptanceStep(
                    name="project",
                    status=AcceptanceStatus.BLOCKED,
                    http_status=None,
                    duration_ms=0,
                    detail={"reason": "synthetic_mutations_not_authorized"},
                ),
                None,
            )

        listed_step, projects = self.json_step(
            "project_list",
            "GET",
            "/projects",
            validate=lambda body: isinstance(body, list),
            failure_reason="invalid_project_list",
        )
        if listed_step.status is not AcceptanceStatus.PASS or not isinstance(projects, list):
            return listed_step, None
        exact = next(
            (item for item in projects if isinstance(item, dict) and item.get("name") == self.project_name),
            None,
        )
        if exact is not None:
            project_id = str(exact.get("id") or "")
            if project_id:
                return (
                    AcceptanceStep(
                        name="project",
                        status=AcceptanceStatus.PASS,
                        http_status=listed_step.http_status,
                        duration_ms=listed_step.duration_ms,
                        detail={"project_id": project_id, "reused": True},
                    ),
                    project_id,
                )

        created_step, project = self.json_step(
            "project",
            "POST",
            "/projects",
            json_body={"name": self.project_name},
            validate=lambda body: isinstance(body, dict)
            and body.get("name") == self.project_name
            and bool(body.get("id")),
            failure_reason="project_creation_contract_failed",
        )
        project_id = str(project.get("id")) if created_step.status is AcceptanceStatus.PASS else None
        return created_step, project_id

    def ensure_documents(
        self,
        project_id: str,
        sources: list[tuple[str, Path]],
    ) -> tuple[list[AcceptanceStep], list[dict[str, Any]]]:
        if not self.allow_synthetic_mutations:
            return (
                [
                    AcceptanceStep(
                        name="documents",
                        status=AcceptanceStatus.BLOCKED,
                        http_status=None,
                        duration_ms=0,
                        detail={"reason": "synthetic_mutations_not_authorized"},
                    )
                ],
                [],
            )
        listed, body = self.json_step(
            "document_list",
            "GET",
            f"/projects/{project_id}/documents",
            validate=lambda value: isinstance(value, list),
            failure_reason="invalid_document_list",
        )
        if listed.status is not AcceptanceStatus.PASS or not isinstance(body, list):
            return [listed], []
        documents = [item for item in body if isinstance(item, dict)]
        by_filename = {str(item.get("filename")): item for item in documents}
        steps: list[AcceptanceStep] = []
        for document_type, path in sources:
            existing = by_filename.get(path.name)
            if existing and existing.get("status") == "completed":
                steps.append(
                    AcceptanceStep(
                        name=f"document:{path.name}",
                        status=AcceptanceStatus.PASS,
                        http_status=listed.http_status,
                        duration_ms=0,
                        detail={"document_id": str(existing.get("id")), "reused": True},
                    )
                )
                continue
            step, uploaded = self._upload_document(project_id, document_type, path)
            steps.append(step)
            if step.status is AcceptanceStatus.PASS and isinstance(uploaded, dict):
                document = uploaded.get("document")
                if isinstance(document, dict):
                    documents.append(document)
                    by_filename[path.name] = document
        return steps, documents

    def _upload_document(
        self,
        project_id: str,
        document_type: str,
        path: Path,
    ) -> tuple[AcceptanceStep, Any]:
        started = perf_counter()
        content_type = "text/csv" if path.suffix.lower() == ".csv" else "text/markdown"
        try:
            response = self.client.post(
                f"{self.api_url}/projects/{project_id}/documents",
                data={"document_type": document_type},
                files={"file": (path.name, path.read_bytes(), content_type)},
                timeout=180,
            )
        except (httpx.HTTPError, OSError) as exc:
            if isinstance(exc, httpx.HTTPError):
                return self._network_failure(f"document:{path.name}", started, exc), None
            return (
                AcceptanceStep(
                    name=f"document:{path.name}",
                    status=AcceptanceStatus.FAIL,
                    http_status=None,
                    duration_ms=round((perf_counter() - started) * 1000),
                    detail={"reason": "source_read_failed", "error_type": type(exc).__name__},
                ),
                None,
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        valid = (
            response.status_code == 201
            and isinstance(body, dict)
            and isinstance(body.get("document"), dict)
            and body["document"].get("filename") == path.name
            and body["document"].get("status") == "completed"
            and isinstance(body.get("ingestion"), dict)
            and body["ingestion"].get("status") == "completed"
        )
        return (
            AcceptanceStep(
                name=f"document:{path.name}",
                status=AcceptanceStatus.PASS if valid else AcceptanceStatus.FAIL,
                http_status=response.status_code,
                duration_ms=round((perf_counter() - started) * 1000),
                detail={} if valid else {"reason": "document_ingestion_contract_failed"},
            ),
            body,
        )
    def run_feature_manifest(
        self,
        project_id: str,
        documents: list[dict[str, Any]],
    ) -> list[AcceptanceStep]:
        """Exercise every non-foundation feature operation using synthetic state."""
        steps: list[AcceptanceStep] = []

        def document(*, filename: str | None = None, document_type: str | None = None) -> dict[str, Any] | None:
            return next(
                (
                    item
                    for item in documents
                    if (filename is None or item.get("filename") == filename)
                    and (document_type is None or item.get("document_type") == document_type)
                    and item.get("status") == "completed"
                ),
                None,
            )

        def add(
            name: str,
            method: str,
            path: str,
            *,
            validate: Callable[[Any], bool],
            failure_reason: str,
            json_body: dict[str, Any] | None = None,
            params: dict[str, Any] | None = None,
            data: dict[str, Any] | None = None,
            files: dict[str, Any] | None = None,
            expected_statuses: set[int] | None = None,
        ) -> Any:
            step, body = self.json_step(
                name,
                method,
                path,
                json_body=json_body,
                params=params,
                data=data,
                files=files,
                expected_statuses=expected_statuses,
                validate=validate,
                failure_reason=failure_reason,
            )
            steps.append(step)
            return body

        def blocked(name: str, reason: str) -> None:
            steps.append(
                AcceptanceStep(
                    name=name,
                    status=AcceptanceStatus.BLOCKED,
                    http_status=None,
                    duration_ms=0,
                    detail={"reason": reason},
                )
            )

        specification = document(filename="Switchgear_Specification.md") or document(document_type="specification")
        submittal = document(filename="SWGR-002_ArcLine_SWGR-A.md") or document(document_type="submittal")
        rfi = document(document_type="RFI")
        schedule = document(document_type="schedule")
        commissioning = document(filename="Switchgear_Procedure_Template.md") or document(document_type="commissioning_record")
        first_document = specification or next(iter(documents), None)

        if first_document:
            document_id = str(first_document["id"])
            add(
                "ingestion_status",
                "GET",
                f"/projects/{project_id}/documents/{document_id}/ingestion",
                validate=lambda body: isinstance(body, dict)
                and (body.get("ingestion") or {}).get("status") == "completed",
                failure_reason="ingestion_status_contract_failed",
            )
            add(
                "completed_ingestion_guard",
                "POST",
                f"/projects/{project_id}/documents/{document_id}/ingest",
                expected_statuses={409},
                validate=lambda body: "completed" in json.dumps(body).lower(),
                failure_reason="completed_ingestion_guard_failed",
            )
        else:
            blocked("ingestion_status", "no_completed_document")
            blocked("completed_ingestion_guard", "no_completed_document")

        question = "What short-circuit rating is required for SWGR-A?"
        add(
            "retrieval",
            "POST",
            f"/projects/{project_id}/retrieve",
            json_body={"query": question, "limit": 12},
            validate=lambda body: isinstance(body, dict) and bool(body.get("results")),
            failure_reason="retrieval_returned_no_evidence",
        )
        add(
            "context",
            "POST",
            f"/projects/{project_id}/context",
            json_body={"query": question, "limit": 12},
            validate=lambda body: isinstance(body, dict) and bool(body.get("chunks")) and body.get("sufficient") is True,
            failure_reason="context_not_sufficient",
        )
        add(
            "copilot_grounding",
            "POST",
            f"/projects/{project_id}/copilot",
            json_body={"question": question, "history": []},
            validate=lambda body: isinstance(body, dict)
            and body.get("status") in {"ANSWERED", "PARTIAL"}
            and bool(body.get("citations"))
            and all(item.get("document_id") and item.get("chunk_id") for item in body.get("citations", []))
            and all(item.get("support_status") != "UNSUPPORTED" for item in body.get("claims", [])),
            failure_reason="copilot_grounding_contract_failed",
        )
        add(
            "query_plan",
            "POST",
            f"/projects/{project_id}/query-plan",
            json_body={"question": question, "history": []},
            validate=lambda body: isinstance(body, dict)
            and body.get("execution_mode") == "advisory_only"
            and isinstance(body.get("plan"), dict),
            failure_reason="query_plan_contract_failed",
        )
        add(
            "rfi_matches",
            "POST",
            f"/projects/{project_id}/rfis/matches",
            json_body={"proposed_rfi": "Confirm the required bypass clearance and access arrangement for UPS-A."},
            validate=lambda body: isinstance(body, dict) and bool(body.get("matches")),
            failure_reason="rfi_match_contract_failed",
        )
        add(
            "graph",
            "GET",
            f"/projects/{project_id}/graph",
            validate=lambda body: isinstance(body, dict)
            and str(body.get("project_id")) == project_id
            and bool(body.get("nodes")),
            failure_reason="graph_contract_failed",
        )

        finding_id: str | None = None
        if specification and submittal:
            compliance = add(
                "compliance_check",
                "POST",
                f"/projects/{project_id}/compliance/checks",
                json_body={"specification_document_id": specification["id"], "submittal_document_id": submittal["id"]},
                validate=lambda body: isinstance(body, dict) and bool(body.get("findings")),
                failure_reason="compliance_check_contract_failed",
            )
            if isinstance(compliance, dict) and compliance.get("findings"):
                finding_id = str(compliance["findings"][0].get("id") or "") or None
        else:
            blocked("compliance_check", "specification_or_submittal_missing")
        findings = add(
            "compliance_findings",
            "GET",
            f"/projects/{project_id}/compliance/findings",
            validate=lambda body: isinstance(body, dict) and bool(body.get("findings")),
            failure_reason="compliance_findings_missing",
        )
        if not finding_id and isinstance(findings, dict) and findings.get("findings"):
            finding_id = str(findings["findings"][0].get("id") or "") or None
        if finding_id:
            add(
                "compliance_review",
                "PATCH",
                f"/projects/{project_id}/compliance/findings/{finding_id}/review",
                json_body={"decision": "needs_review", "reviewer_note": "Synthetic production acceptance review"},
                validate=lambda body: isinstance(body, dict) and body.get("review_status") == "needs_review",
                failure_reason="compliance_review_contract_failed",
            )
        else:
            blocked("compliance_review", "compliance_finding_missing")
        add(
            "compliance_evaluation",
            "GET",
            f"/projects/{project_id}/compliance/evaluation",
            validate=lambda body: isinstance(body, dict) and bool(body),
            failure_reason="compliance_evaluation_contract_failed",
        )

        if schedule:
            add(
                "schedule_analysis",
                "POST",
                f"/projects/{project_id}/schedule/analysis",
                json_body={"schedule_document_id": schedule["id"], "analysis_date": "2026-08-23"},
                validate=lambda body: isinstance(body, dict) and bool(body.get("risks")) and bool(body.get("snapshot")),
                failure_reason="schedule_analysis_contract_failed",
            )
            add(
                "schedule_snapshots",
                "GET",
                f"/projects/{project_id}/schedule/snapshots",
                params={"schedule_document_id": schedule["id"]},
                validate=lambda body: isinstance(body, list) and bool(body),
                failure_reason="schedule_snapshot_missing",
            )
        else:
            blocked("schedule_analysis", "schedule_document_missing")
            blocked("schedule_snapshots", "schedule_document_missing")

        record_id: str | None = None
        if commissioning:
            procedure = add(
                "commissioning_procedure",
                "GET",
                f"/projects/{project_id}/commissioning/procedures/{commissioning['id']}",
                validate=lambda body: isinstance(body, dict) and bool(body.get("steps")),
                failure_reason="commissioning_procedure_contract_failed",
            )
            observations = []
            if isinstance(procedure, dict) and procedure.get("steps"):
                observations = [{"step_index": procedure["steps"][0]["index"], "observation": "Synthetic acceptance observation"}]
            record = add(
                "commissioning_record",
                "POST",
                f"/projects/{project_id}/commissioning/records",
                json_body={"procedure_document_id": commissioning["id"], "observations": observations},
                validate=lambda body: isinstance(body, dict) and bool(body.get("id")) and bool(body.get("status")),
                failure_reason="commissioning_record_contract_failed",
            )
            if isinstance(record, dict):
                record_id = str(record.get("id") or "") or None
        else:
            blocked("commissioning_procedure", "commissioning_document_missing")
            blocked("commissioning_record", "commissioning_document_missing")
        if record_id:
            add(
                "commissioning_record_read",
                "GET",
                f"/projects/{project_id}/commissioning/records/{record_id}",
                validate=lambda body: isinstance(body, dict) and str(body.get("id")) == record_id,
                failure_reason="commissioning_record_read_failed",
            )
        else:
            blocked("commissioning_record_read", "commissioning_record_missing")
        add(
            "commissioning_readiness",
            "GET",
            f"/projects/{project_id}/commissioning/readiness/SWGR-A",
            validate=lambda body: isinstance(body, dict)
            and body.get("equipment_id") == "SWGR-A"
            and isinstance(body.get("score"), int),
            failure_reason="commissioning_readiness_contract_failed",
        )

        add(
            "procurement_dashboard",
            "POST",
            f"/projects/{project_id}/procurement/dashboard",
            json_body={
                "items": [
                    {
                        "equipment_tag": "SWGR-A",
                        "vendor": "Synthetic Acceptance Vendor",
                        "purchase_order_status": "placed",
                        "planned_delivery": "2026-05-20",
                        "forecast_delivery": "2026-06-24",
                        "lead_time_days": 42,
                    }
                ]
            },
            validate=lambda body: isinstance(body, dict)
            and body.get("mode") == "demo_mock"
            and body.get("live_data_available") is False,
            failure_reason="procurement_truth_contract_failed",
        )
        seeded = add(
            "supply_chain_seed",
            "POST",
            f"/projects/{project_id}/supply-chain/seed",
            validate=lambda body: isinstance(body, dict) and body.get("synthetic_simulation") is True and bool(body.get("shipments")),
            failure_reason="supply_chain_seed_contract_failed",
        )
        shipment_id: str | None = None
        if isinstance(seeded, dict) and seeded.get("shipments"):
            shipment_id = str(seeded["shipments"][0].get("shipment_id") or "") or None

        import_csv = (
            "equipment_id,vendor,planned_date,current_eta,required_on_site_date,status,location\n"
            "SWGR-A,Synthetic Acceptance Vendor,2026-05-20,2026-06-24,2026-05-27,at_risk,Synthetic factory\n"
        ).encode()
        imported = add(
            "supply_chain_import",
            "POST",
            f"/projects/{project_id}/supply-chain/import",
            files={"file": ("acceptance_shipments.csv", import_csv, "text/csv")},
            validate=lambda body: isinstance(body, dict)
            and body.get("imported") == 1
            and body.get("live_tracking") is False
            and bool(body.get("assessments")),
            failure_reason="supply_chain_import_contract_failed",
        )
        imported_id: str | None = None
        imported_event_id: str | None = None
        if isinstance(imported, dict) and imported.get("assessments"):
            imported_id = str(imported["assessments"][0].get("shipment_id") or "") or None
            imported_event_id = str(imported["assessments"][0].get("impact_event_id") or "") or None
        add(
            "supply_chain_assessments",
            "GET",
            f"/projects/{project_id}/supply-chain/assessments",
            validate=lambda body: isinstance(body, list) and bool(body),
            failure_reason="supply_chain_assessments_missing",
        )
        if imported_id:
            assessed = add(
                "supply_chain_reassessment",
                "POST",
                f"/projects/{project_id}/supply-chain/shipments/{imported_id}/assessment",
                validate=lambda body: isinstance(body, dict) and str(body.get("shipment_id")) == imported_id,
                failure_reason="supply_chain_reassessment_failed",
            )
            if not imported_event_id and isinstance(assessed, dict):
                imported_event_id = str(assessed.get("impact_event_id") or "") or None
        else:
            blocked("supply_chain_reassessment", "imported_shipment_missing")
        add(
            "supply_chain_alerts",
            "GET",
            f"/projects/{project_id}/supply-chain/alerts",
            validate=lambda body: isinstance(body, list),
            failure_reason="supply_chain_alerts_contract_failed",
        )
        if imported_id:
            add(
                "supply_chain_timeline",
                "GET",
                f"/projects/{project_id}/supply-chain/shipments/{imported_id}/timeline",
                validate=lambda body: isinstance(body, dict)
                and str(body.get("shipment_id")) == imported_id
                and bool(body.get("events")),
                failure_reason="supply_chain_timeline_contract_failed",
            )
        else:
            blocked("supply_chain_timeline", "imported_shipment_missing")
        add(
            "demo_reset",
            "POST",
            f"/projects/{project_id}/demo/reset",
            validate=lambda body: isinstance(body, dict) and body.get("synthetic_simulation") is True and bool(body.get("shipments")),
            failure_reason="demo_reset_contract_failed",
        )
        vertical = add(
            "vertical_scenario",
            "POST",
            f"/projects/{project_id}/demo/vertical-scenario",
            validate=lambda body: isinstance(body, dict)
            and body.get("synthetic_data") is True
            and bool(body.get("compliance_finding"))
            and bool(body.get("mitigation"))
            and bool(body.get("digital_thread")),
            failure_reason="vertical_scenario_contract_failed",
        )
        if isinstance(vertical, dict):
            finding_id = str((vertical.get("compliance_finding") or {}).get("id") or finding_id or "") or None
            shipment_id = str((vertical.get("shipment_risk") or {}).get("shipment_id") or shipment_id or "") or None
            imported_event_id = str((vertical.get("shipment_risk") or {}).get("impact_event_id") or imported_event_id or "") or None
        add(
            "executive_summary",
            "GET",
            f"/projects/{project_id}/executive-summary",
            validate=lambda body: isinstance(body, dict) and str(body.get("project_id")) == project_id and body.get("synthetic_data") is True,
            failure_reason="executive_summary_contract_failed",
        )
        shipment_list = add(
            "supply_chain_shipments",
            "GET",
            f"/projects/{project_id}/supply-chain/shipments",
            validate=lambda body: isinstance(body, dict) and body.get("synthetic_simulation") is True and bool(body.get("shipments")),
            failure_reason="supply_chain_shipments_missing",
        )
        if not shipment_id and isinstance(shipment_list, dict) and shipment_list.get("shipments"):
            shipment_id = str(shipment_list["shipments"][0].get("shipment_id") or "") or None
        if shipment_id:
            add(
                "supply_chain_risk_event",
                "POST",
                f"/projects/{project_id}/supply-chain/shipments/{shipment_id}/risk-events",
                json_body={
                    "event_type": "synthetic_acceptance_hold",
                    "description": "Synthetic acceptance-only risk event",
                    "occurred_at": "2026-08-23T12:00:00Z",
                    "alert_generated_at": "2026-08-23T12:12:00Z",
                    "forecast_delay_days": 18,
                },
                validate=lambda body: isinstance(body, dict)
                and str(body.get("shipment_id")) == shipment_id
                and body.get("synthetic_simulation") is True,
                failure_reason="supply_chain_risk_event_contract_failed",
            )
            add(
                "supply_chain_risk",
                "GET",
                f"/projects/{project_id}/supply-chain/shipments/{shipment_id}/risk",
                validate=lambda body: isinstance(body, dict)
                and str(body.get("shipment_id")) == shipment_id
                and body.get("synthetic_simulation") is True,
                failure_reason="supply_chain_risk_contract_failed",
            )
            add(
                "supply_chain_alternatives",
                "GET",
                f"/projects/{project_id}/supply-chain/shipments/{shipment_id}/alternatives",
                validate=lambda body: isinstance(body, dict)
                and str(body.get("shipment_id")) == shipment_id
                and body.get("synthetic_simulation") is True,
                failure_reason="supply_chain_alternatives_contract_failed",
            )
        else:
            for name in ("supply_chain_risk_event", "supply_chain_risk", "supply_chain_alternatives"):
                blocked(name, "synthetic_shipment_missing")

        add(
            "digital_thread",
            "GET",
            f"/projects/{project_id}/equipment/SWGR-A/digital-thread",
            validate=lambda body: isinstance(body, dict)
            and str(body.get("project_id")) == project_id
            and (body.get("equipment") or {}).get("equipment_id") == "SWGR-A",
            failure_reason="digital_thread_contract_failed",
        )
        add(
            "equipment_impact_event",
            "POST",
            f"/projects/{project_id}/equipment/SWGR-A/impact-chain/events",
            json_body={
                "type": "DELIVERY_RISK",
                "source_id": shipment_id or "synthetic-acceptance",
                "severity": "high",
                "confidence": 1.0,
                "timestamp": "2026-08-23T12:00:00Z",
                "assumptions": {"schedule_impact_days": 7, "commissioning_impact_days": 0},
                "evidence": [],
            },
            validate=lambda body: isinstance(body, dict) and body.get("equipment_id") == "SWGR-A" and bool(body.get("events")),
            failure_reason="equipment_impact_event_contract_failed",
        )
        add(
            "equipment_impact_chain",
            "GET",
            f"/projects/{project_id}/equipment/SWGR-A/impact-chain",
            validate=lambda body: isinstance(body, dict) and body.get("equipment_id") == "SWGR-A" and bool(body.get("events")),
            failure_reason="equipment_impact_chain_contract_failed",
        )

        chain_id: str | None = None
        if finding_id and shipment_id and schedule:
            chain = add(
                "impact_chain_start",
                "POST",
                f"/projects/{project_id}/impact-chains",
                json_body={
                    "compliance_finding_id": finding_id,
                    "shipment_id": shipment_id,
                    "schedule_document_id": schedule["id"],
                    "replacement_lead_time_days": 42,
                    "replacement_cost": 85000,
                    "analysis_date": "2026-08-23",
                },
                validate=lambda body: isinstance(body, dict)
                and bool(body.get("chain_id"))
                and body.get("status") == "AWAITING_HUMAN_DECISION"
                and bool(body.get("mitigation_scenarios")),
                failure_reason="impact_chain_start_contract_failed",
            )
            if isinstance(chain, dict):
                chain_id = str(chain.get("chain_id") or "") or None
        else:
            blocked("impact_chain_start", "impact_chain_inputs_missing")
        if chain_id:
            add(
                "impact_chain_decision",
                "POST",
                f"/projects/{project_id}/impact-chains/{chain_id}/decision",
                json_body={"action": "REQUEST_REVIEW", "note": "Synthetic acceptance review"},
                validate=lambda body: isinstance(body, dict)
                and str(body.get("chain_id")) == chain_id
                and body.get("status") == "ACTION_CREATED"
                and (body.get("approved_action") or {}).get("action") == "REQUEST_REVIEW",
                failure_reason="impact_chain_decision_contract_failed",
            )
        else:
            blocked("impact_chain_decision", "impact_chain_missing")

        evaluation = add(
            "evaluation_run",
            "POST",
            "/api/evaluation/run",
            json_body={"project_id": project_id, "fixture_name": "synthetic_small", "fixture_format": "json"},
            validate=lambda body: isinstance(body, dict) and bool(body.get("id")) and body.get("status") in {"COMPLETED", "FAILED"},
            failure_reason="evaluation_run_contract_failed",
        )
        evaluation_id = str(evaluation.get("id") or "") if isinstance(evaluation, dict) else ""
        if evaluation_id:
            add(
                "evaluation_read",
                "GET",
                f"/api/evaluation/runs/{evaluation_id}",
                params={"project_id": project_id},
                validate=lambda body: isinstance(body, dict) and str(body.get("id")) == evaluation_id,
                failure_reason="evaluation_read_contract_failed",
            )
        else:
            blocked("evaluation_read", "evaluation_run_missing")

        simulation_id: str | None = None
        if shipment_id and imported_event_id:
            simulation = add(
                "mitigation_simulation",
                "POST",
                "/api/mitigations/simulate",
                json_body={
                    "project_id": project_id,
                    "shipment_id": shipment_id,
                    "impact_event_id": imported_event_id,
                    "rules": {"expedite_recovery_days": 18, "resequence_recovery_days": 10},
                },
                validate=lambda body: isinstance(body, dict) and bool(body.get("simulation_id")) and bool(body.get("scenarios")),
                failure_reason="mitigation_simulation_contract_failed",
            )
            if isinstance(simulation, dict):
                simulation_id = str(simulation.get("simulation_id") or "") or None
        else:
            blocked("mitigation_simulation", "mitigation_inputs_missing")
        if simulation_id:
            add(
                "mitigation_selection",
                "POST",
                f"/api/mitigations/{simulation_id}/select",
                json_body={"project_id": project_id, "scenario_key": "expedite_shipment", "reviewer_note": "Synthetic acceptance selection"},
                validate=lambda body: isinstance(body, dict)
                and str(body.get("simulation_id")) == simulation_id
                and (body.get("selected") or {}).get("key") == "expedite_shipment",
                failure_reason="mitigation_selection_contract_failed",
            )
        else:
            blocked("mitigation_selection", "mitigation_simulation_missing")

        add(
            "benchmark_create",
            "POST",
            "/api/benchmarks",
            json_body={
                "project_id": project_id,
                "workflow_type": "rfi_search",
                "manual_baseline_seconds": 600,
                "atlas_execution_seconds": 60,
                "measurement_source": "synthetic production acceptance",
                "sample_count": 1,
                "measurement_kind": "measured",
                "timestamp": "2026-08-23T12:00:00Z",
                "synthetic_data": True,
            },
            validate=lambda body: isinstance(body, dict) and str(body.get("project_id")) == project_id and body.get("synthetic_data") is True,
            failure_reason="benchmark_create_contract_failed",
        )
        add(
            "benchmark_summary",
            "GET",
            "/api/benchmarks/summary",
            params={"project_id": project_id},
            validate=lambda body: isinstance(body, dict)
            and str(body.get("project_id")) == project_id
            and int(body.get("record_count", 0)) >= 1
            and body.get("synthetic_data_present") is True,
            failure_reason="benchmark_summary_contract_failed",
        )
        return steps

    def json_step(
        self,
        name: str,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
        validate: Callable[[Any], bool],
        failure_reason: str,
    ) -> tuple[AcceptanceStep, Any]:
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and not self.allow_synthetic_mutations:
            return (
                AcceptanceStep(
                    name=name,
                    status=AcceptanceStatus.BLOCKED,
                    http_status=None,
                    duration_ms=0,
                    detail={"reason": "synthetic_mutations_not_authorized"},
                ),
                None,
            )
        started = perf_counter()
        try:
            response = self.client.request(
                method,
                f"{self.api_url}{path}",
                json=json_body,
                params=params,
                data=data,
                files=files,
                timeout=120,
            )
        except httpx.HTTPError as exc:
            return self._network_failure(name, started, exc), None
        try:
            body = response.json()
        except ValueError:
            body = None
        duration_ms = round((perf_counter() - started) * 1000)
        status_ok = response.status_code in expected_statuses if expected_statuses is not None else response.is_success
        if not status_ok:
            return (
                AcceptanceStep(
                    name=name,
                    status=AcceptanceStatus.FAIL,
                    http_status=response.status_code,
                    duration_ms=duration_ms,
                    detail={"reason": "http_error"},
                ),
                body,
            )
        try:
            valid = validate(body)
        except Exception as exc:
            return (
                AcceptanceStep(
                    name=name,
                    status=AcceptanceStatus.FAIL,
                    http_status=response.status_code,
                    duration_ms=duration_ms,
                    detail={"reason": "semantic_validation_error", "error_type": type(exc).__name__},
                ),
                body,
            )
        if not valid:
            return (
                AcceptanceStep(
                    name=name,
                    status=AcceptanceStatus.FAIL,
                    http_status=response.status_code,
                    duration_ms=duration_ms,
                    detail={"reason": failure_reason},
                ),
                body,
            )
        return (
            AcceptanceStep(
                name=name,
                status=AcceptanceStatus.PASS,
                http_status=response.status_code,
                duration_ms=duration_ms,
                detail={},
            ),
            body,
        )


def execute_acceptance(
    runner: Any,
    *,
    deployed_sha: str,
    output: Path,
    sources: list[tuple[str, Path]],
) -> tuple[Path, Path, AcceptanceReport]:
    steps, project_id = runner.run_foundation()
    all_steps = list(steps)
    if project_id:
        document_steps, documents = runner.ensure_documents(project_id, sources)
        all_steps.extend(document_steps)
        all_steps.extend(runner.run_feature_manifest(project_id, documents))
    else:
        all_steps.append(
            AcceptanceStep(
                name="feature_manifest",
                status=AcceptanceStatus.BLOCKED,
                http_status=None,
                duration_ms=0,
                detail={"reason": "synthetic_project_unavailable"},
            )
        )
    report = AcceptanceReport(
        deployed_sha=deployed_sha,
        api_url=runner.api_url,
        frontend_url=runner.frontend_url,
        project_name=runner.project_name,
        project_id=project_id,
        steps=all_steps,
    )
    json_path, markdown_path = report.write(output)
    return json_path, markdown_path, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--frontend-url", required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--deployed-sha", default="unverified")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-synthetic-mutations", action="store_true")
    args = parser.parse_args(argv)

    if __package__:
        from scripts.seed_demo import sources
    else:
        from seed_demo import sources

    with httpx.Client(follow_redirects=True, timeout=180) as client:
        runner = AcceptanceRunner(
            api_url=args.api_url,
            frontend_url=args.frontend_url,
            project_name=args.project_name,
            allow_synthetic_mutations=args.allow_synthetic_mutations,
            client=client,
        )
        json_path, markdown_path, report = execute_acceptance(
            runner,
            deployed_sha=args.deployed_sha,
            output=args.output,
            sources=sources(),
        )
    print(
        json.dumps(
            {
                "json_report": str(json_path),
                "markdown_report": str(markdown_path),
                "summary": report._payload()["summary"],
            },
            sort_keys=True,
        )
    )
    return int(
        any(
            step.status in {AcceptanceStatus.FAIL, AcceptanceStatus.BLOCKED}
            for step in report.steps
            if isinstance(step, AcceptanceStep)
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
