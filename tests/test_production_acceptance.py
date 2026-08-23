import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from scripts.production_acceptance import (
    AcceptanceReport,
    AcceptanceRunner,
    AcceptanceStep,
    AcceptanceStatus,
    execute_acceptance,
    sanitize_detail,
    valid_evaluation_terminal,
)


API_URL = "https://api.example"
FRONTEND_URL = "https://atlas.example"
CANARY_NAME = "Atlas Production Canary 2026-08-23-deadbee"


def test_direct_cli_can_load_the_synthetic_corpus_module(tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/production_acceptance.py",
            "--api-url",
            API_URL,
            "--frontend-url",
            FRONTEND_URL,
            "--project-name",
            "not-a-synthetic-canary",
            "--output",
            str(tmp_path / "report.json"),
        ],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "project name must identify an Atlas synthetic canary" in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


def runner(
    handler,
    *,
    project_name: str = CANARY_NAME,
    allow_synthetic_mutations: bool = True,
) -> tuple[AcceptanceRunner, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return (
        AcceptanceRunner(
            api_url=API_URL,
            frontend_url=FRONTEND_URL,
            project_name=project_name,
            allow_synthetic_mutations=allow_synthetic_mutations,
            client=client,
        ),
        client,
    )


def test_mutating_steps_require_explicit_synthetic_permission() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500, json={"error": "network call must not occur"})

    acceptance, client = runner(handler, allow_synthetic_mutations=False)
    with client:
        step, project_id = acceptance.ensure_project()

    assert project_id is None
    assert step.status is AcceptanceStatus.BLOCKED
    assert step.detail == {"reason": "synthetic_mutations_not_authorized"}
    assert requests == []


def test_project_selection_never_falls_back_to_an_existing_project() -> None:
    posted_names: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/projects":
            return httpx.Response(
                200,
                json=[
                    {"id": "customer-project", "name": "Customer Project"},
                    {"id": "old-canary", "name": "Atlas Production Canary 2026-08-22-old"},
                ],
            )
        if request.method == "POST" and request.url.path == "/projects":
            posted_names.append(json.loads(request.content)["name"])
            return httpx.Response(201, json={"id": "new-canary", "name": CANARY_NAME})
        return httpx.Response(404)

    acceptance, client = runner(handler)
    with client:
        step, project_id = acceptance.ensure_project()

    assert step.status is AcceptanceStatus.PASS
    assert project_id == "new-canary"
    assert posted_names == [CANARY_NAME]


def test_resume_reuses_only_the_exact_canary_name() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json=[
                {"id": "customer-project", "name": "Customer Project"},
                {"id": "exact-canary", "name": CANARY_NAME},
            ],
        )

    acceptance, client = runner(handler)
    with client:
        step, project_id = acceptance.ensure_project()

    assert step.status is AcceptanceStatus.PASS
    assert project_id == "exact-canary"
    assert methods == ["GET"]


def test_non_canary_project_name_rejects_before_any_network_call() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    with pytest.raises(ValueError, match="synthetic canary"):
        runner(handler, project_name="Customer Project")

    assert requests == []


def test_failed_semantic_assertion_is_not_reported_as_pass() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ANSWERED", "citations": []})

    acceptance, client = runner(handler)
    with client:
        step, _ = acceptance.json_step(
            "copilot_grounding",
            "POST",
            "/projects/exact-canary/copilot",
            json_body={"question": "What rating is required?", "history": []},
            validate=lambda body: bool(body.get("citations")),
            failure_reason="missing_citations",
        )

    assert step.status is AcceptanceStatus.FAIL
    assert step.http_status == 200
    assert step.detail == {"reason": "missing_citations"}


@pytest.mark.parametrize("status", ["COMPLETED", "COMPLETED_WITH_FAILURES", "FAILED"])
def test_evaluation_terminal_contract_accepts_every_persisted_terminal_status(status: str) -> None:
    assert valid_evaluation_terminal({"id": "run-1", "status": status})


def test_report_redacts_content_and_raw_provider_errors() -> None:
    sanitized = sanitize_detail(
        {
            "status": 502,
            "document_id": "doc-123",
            "prompt": "private prompt text",
            "completion": "private completion text",
            "content": "private document text",
            "error": {"message": "raw provider body", "code": "upstream_timeout"},
            "api_key": "secret-key",
        }
    )

    encoded = json.dumps(sanitized, sort_keys=True)
    assert sanitized == {
        "document_id": "doc-123",
        "error": {"code": "upstream_timeout"},
        "status": 502,
    }
    assert "private" not in encoded
    assert "secret-key" not in encoded
    assert "raw provider body" not in encoded


def test_foundation_checks_validate_live_contract_and_reuse_exact_canary() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.host == "atlas.example":
            return httpx.Response(200, text="<h1>Project Atlas</h1><p>EPC project intelligence</p>")
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "components": {"api": "ok"}})
        if request.url.path == "/ready":
            return httpx.Response(
                200,
                json={"status": "ok", "components": {"api": "ok", "database": "ok", "qdrant": "ok"},},
            )
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json={"openapi": "3.1.0", "paths": {"/projects": {"get": {}}}})
        if request.method == "OPTIONS" and request.url.path == "/projects":
            return httpx.Response(200, headers={"access-control-allow-origin": FRONTEND_URL})
        if request.url.path == "/projects":
            return httpx.Response(200, json=[{"id": "exact-canary", "name": CANARY_NAME}])
        return httpx.Response(404)

    acceptance, client = runner(handler)
    with client:
        steps, project_id = acceptance.run_foundation()

    assert project_id == "exact-canary"
    assert {step.name: step.status for step in steps} == {
        "liveness": AcceptanceStatus.PASS,
        "readiness": AcceptanceStatus.PASS,
        "openapi": AcceptanceStatus.PASS,
        "cors": AcceptanceStatus.PASS,
        "frontend_identity": AcceptanceStatus.PASS,
        "project": AcceptanceStatus.PASS,
    }
    assert calls == [
        ("GET", "/health"),
        ("GET", "/ready"),
        ("GET", "/openapi.json"),
        ("OPTIONS", "/projects"),
        ("GET", "/"),
        ("GET", "/projects"),
    ]


def test_acceptance_report_writes_sanitized_json_and_markdown(tmp_path) -> None:
    report = AcceptanceReport(
        deployed_sha="deadbeef",
        api_url=API_URL,
        frontend_url=FRONTEND_URL,
        project_name=CANARY_NAME,
        project_id="project-123",
        steps=[
            {
                "name": "provider_probe",
                "status": "FAIL",
                "http_status": 502,
                "duration_ms": 12,
                "detail": {"error": {"code": "timeout", "message": "raw provider body"}, "prompt": "secret"},
            }
        ],
    )

    json_path, markdown_path = report.write(tmp_path / "baseline.json")
    json_text = json_path.read_text(encoding="utf-8")
    markdown_text = markdown_path.read_text(encoding="utf-8")

    assert json.loads(json_text)["summary"] == {"FAIL": 1}
    assert "deadbeef" in markdown_text
    assert "provider_probe" in markdown_text
    assert "raw provider body" not in json_text + markdown_text
    assert "secret" not in json_text + markdown_text


def test_document_seed_reuses_completed_source_and_uploads_only_missing_file(tmp_path) -> None:
    existing = tmp_path / "existing.md"
    missing = tmp_path / "missing.csv"
    existing.write_text("existing synthetic evidence", encoding="utf-8")
    missing.write_text("task_id,name\nT-1,Synthetic task\n", encoding="utf-8")
    uploaded: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "doc-existing",
                        "project_id": "exact-canary",
                        "filename": "existing.md",
                        "document_type": "specification",
                        "status": "completed",
                        "page_count": 1,
                        "metadata": {},
                    }
                ],
            )
        uploaded.append(request.content)
        return httpx.Response(
            201,
            json={
                "document": {
                    "id": "doc-missing",
                    "project_id": "exact-canary",
                    "filename": "missing.csv",
                    "document_type": "schedule",
                    "status": "completed",
                    "page_count": 1,
                    "metadata": {},
                },
                "ingestion": {
                    "id": "job-missing",
                    "document_id": "doc-missing",
                    "status": "completed",
                    "chunk_count": 1,
                    "attempt_count": 1,
                    "error": None,
                },
            },
        )

    acceptance, client = runner(handler)
    with client:
        steps, documents = acceptance.ensure_documents(
            "exact-canary",
            [("specification", existing), ("schedule", missing)],
        )

    assert [step.status for step in steps] == [AcceptanceStatus.PASS, AcceptanceStatus.PASS]
    assert {item["id"] for item in documents} == {"doc-existing", "doc-missing"}
    assert len(uploaded) == 1
    assert b'missing.csv' in uploaded[0]
    assert b'name="document_type"' in uploaded[0]


def test_feature_manifest_exercises_every_project_feature_operation() -> None:
    calls: list[tuple[str, str]] = []
    finding = {"id": "finding-1", "status": "NON_COMPLIANT", "review_status": "pending"}
    shipment = {"shipment_id": "shipment-1", "equipment_id": "SWGR-A", "reference": "SYN-SHP-001"}

    def handler(request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        calls.append((method, path))
        if path.endswith("/ingestion"):
            return httpx.Response(
                200,
                json={
                    "document": {"id": "doc-spec", "status": "completed"},
                    "ingestion": {"id": "job-1", "status": "completed", "chunk_count": 2},
                },
            )
        if path.endswith("/ingest"):
            return httpx.Response(409, json={"detail": "Document ingestion is already completed"})
        if path.endswith("/retrieve"):
            return httpx.Response(200, json={"results": [{"chunk_id": "chunk-1", "document_id": "doc-spec"}]})
        if path.endswith("/context"):
            return httpx.Response(200, json={"chunks": [{"chunk_id": "chunk-1"}], "sufficient": True})
        if path.endswith("/copilot"):
            return httpx.Response(
                200,
                json={
                    "status": "ANSWERED",
                    "citations": [{"document_id": "doc-spec", "chunk_id": "chunk-1"}],
                    "claims": [{"support_status": "SUPPORTED"}],
                },
            )
        if path.endswith("/query-plan"):
            return httpx.Response(200, json={"execution_mode": "advisory_only", "plan": {"intent": "knowledge_query"}})
        if path.endswith("/rfis/matches"):
            return httpx.Response(200, json={"matches": [{"label": "POTENTIAL_MATCH", "citation": {"document_id": "doc-rfi"}}]})
        if path.endswith("/graph"):
            return httpx.Response(200, json={"project_id": "exact-canary", "nodes": [{"id": "SWGR-A"}], "edges": []})
        if path.endswith("/compliance/checks") or path.endswith("/compliance/findings"):
            return httpx.Response(200, json={"findings": [finding]})
        if "/compliance/findings/" in path:
            return httpx.Response(200, json={**finding, "review_status": "needs_review"})
        if path.endswith("/compliance/evaluation"):
            return httpx.Response(200, json={"precision": 1.0, "recall": 1.0})
        if path.endswith("/schedule/analysis"):
            return httpx.Response(200, json={"risks": [{"affected_task": "T-140"}], "snapshot": {"snapshot_id": "snap-1"}})
        if path.endswith("/schedule/snapshots"):
            return httpx.Response(200, json=[{"snapshot_id": "snap-1", "tasks": []}])
        if "/commissioning/procedures/" in path:
            return httpx.Response(200, json={"document_id": "doc-comm", "equipment_id": "SWGR-A", "steps": [{"index": 0}]})
        if path.endswith("/commissioning/records"):
            return httpx.Response(201, json={"id": "record-1", "status": "needs_review", "steps": []})
        if "/commissioning/records/" in path:
            return httpx.Response(200, json={"id": "record-1", "status": "needs_review", "steps": []})
        if "/commissioning/readiness/" in path:
            return httpx.Response(200, json={"equipment_id": "SWGR-A", "score": 75, "status": "NEEDS_REVIEW"})
        if path.endswith("/procurement/dashboard"):
            return httpx.Response(200, json={"mode": "demo_mock", "live_data_available": False, "cards": []})
        if path.endswith("/demo/vertical-scenario"):
            return httpx.Response(
                200,
                json={
                    "synthetic_data": True,
                    "equipment_id": "SWGR-A",
                    "compliance_finding": finding,
                    "shipment_risk": {**shipment, "impact_event_id": "event-1"},
                    "impact_chain": {"events": [{"id": "event-1", "type": "DELIVERY_RISK"}]},
                    "mitigation": {
                        "simulation_id": "simulation-1",
                        "scenarios": [{"id": "scenario-1", "key": "expedite_shipment"}],
                    },
                    "recommended_mitigation": {"id": "scenario-1", "key": "expedite_shipment"},
                    "digital_thread": {"project_id": "exact-canary"},
                },
            )
        if path.endswith("/demo/reset") or path.endswith("/supply-chain/seed") or path.endswith("/supply-chain/shipments"):
            return httpx.Response(200, json={"synthetic_simulation": True, "shipments": [shipment]})
        if path.endswith("/supply-chain/import"):
            return httpx.Response(
                201,
                json={
                    "filename": "acceptance_shipments.csv",
                    "imported": 1,
                    "assessments": [{"shipment_id": "imported-1", "impact_event_id": "import-event-1"}],
                    "live_tracking": False,
                },
            )
        if path.endswith("/supply-chain/assessments") or path.endswith("/supply-chain/alerts"):
            return httpx.Response(200, json=[{"shipment_id": "imported-1", "severity": "high"}])
        if path.endswith("/assessment"):
            return httpx.Response(200, json={"shipment_id": "imported-1", "impact_event_id": "import-event-1"})
        if path.endswith("/timeline"):
            return httpx.Response(200, json={"shipment_id": "imported-1", "events": [{"id": "timeline-1"}]})
        if path.endswith("/risk-events"):
            return httpx.Response(201, json={"event_id": "risk-1", "shipment_id": "shipment-1", "synthetic_simulation": True})
        if path.endswith("/risk"):
            return httpx.Response(200, json={**shipment, "synthetic_simulation": True, "risk_events": []})
        if path.endswith("/alternatives"):
            return httpx.Response(200, json={"shipment_id": "shipment-1", "synthetic_simulation": True, "options": []})
        if path.endswith("/executive-summary"):
            return httpx.Response(200, json={"project_id": "exact-canary", "synthetic_data": True})
        if path.endswith("/digital-thread"):
            return httpx.Response(200, json={"project_id": "exact-canary", "equipment": {"equipment_id": "SWGR-A"}})
        if path.endswith("/impact-chain/events") or path.endswith("/impact-chain"):
            return httpx.Response(201 if method == "POST" else 200, json={"equipment_id": "SWGR-A", "events": [{"id": "event-1"}], "edges": []})
        if path.endswith("/impact-chains"):
            return httpx.Response(
                201,
                json={
                    "chain_id": "chain-1",
                    "status": "AWAITING_HUMAN_DECISION",
                    "mitigation_scenarios": [{"id": "impact-scenario-1"}],
                },
            )
        if path.endswith("/decision"):
            return httpx.Response(200, json={"chain_id": "chain-1", "status": "ACTION_CREATED", "approved_action": {"action": "REQUEST_REVIEW"}})
        if path == "/api/evaluation/run":
            return httpx.Response(201, json={"id": "evaluation-1", "status": "COMPLETED", "cases": []})
        if path.startswith("/api/evaluation/runs/"):
            return httpx.Response(200, json={"id": "evaluation-1", "status": "COMPLETED", "cases": []})
        if path == "/api/mitigations/simulate":
            return httpx.Response(201, json={"simulation_id": "simulation-1", "scenarios": [{"id": "scenario-1", "key": "expedite_shipment"}]})
        if path.endswith("/select"):
            return httpx.Response(200, json={"simulation_id": "simulation-1", "selected": {"key": "expedite_shipment"}})
        if path == "/api/benchmarks":
            return httpx.Response(201, json={"id": "benchmark-1", "project_id": "exact-canary", "synthetic_data": True})
        if path == "/api/benchmarks/summary":
            return httpx.Response(200, json={"project_id": "exact-canary", "record_count": 1, "synthetic_data_present": True})
        return httpx.Response(404, json={"error": {"code": "unhandled_test_route"}})

    documents = [
        {"id": "doc-spec", "filename": "Switchgear_Specification.md", "document_type": "specification", "status": "completed"},
        {"id": "doc-sub", "filename": "SWGR-002_ArcLine_SWGR-A.md", "document_type": "submittal", "status": "completed"},
        {"id": "doc-rfi", "filename": "RFI-001.md", "document_type": "RFI", "status": "completed"},
        {"id": "doc-schedule", "filename": "atlas_demo_schedule.csv", "document_type": "schedule", "status": "completed"},
        {"id": "doc-comm", "filename": "Switchgear_Procedure_Template.md", "document_type": "commissioning_record", "status": "completed"},
    ]
    acceptance, client = runner(handler)
    with client:
        steps = acceptance.run_feature_manifest("exact-canary", documents)

    assert all(step.status is AcceptanceStatus.PASS for step in steps)
    assert len(steps) == 43
    expected = {
        ("GET", "/projects/exact-canary/documents/doc-spec/ingestion"),
        ("POST", "/projects/exact-canary/documents/doc-spec/ingest"),
        ("POST", "/projects/exact-canary/retrieve"),
        ("POST", "/projects/exact-canary/context"),
        ("POST", "/projects/exact-canary/copilot"),
        ("POST", "/projects/exact-canary/query-plan"),
        ("POST", "/projects/exact-canary/rfis/matches"),
        ("GET", "/projects/exact-canary/graph"),
        ("POST", "/projects/exact-canary/compliance/checks"),
        ("GET", "/projects/exact-canary/compliance/findings"),
        ("PATCH", "/projects/exact-canary/compliance/findings/finding-1/review"),
        ("GET", "/projects/exact-canary/compliance/evaluation"),
        ("POST", "/projects/exact-canary/schedule/analysis"),
        ("GET", "/projects/exact-canary/schedule/snapshots"),
        ("GET", "/projects/exact-canary/commissioning/procedures/doc-comm"),
        ("POST", "/projects/exact-canary/commissioning/records"),
        ("GET", "/projects/exact-canary/commissioning/records/record-1"),
        ("GET", "/projects/exact-canary/commissioning/readiness/SWGR-A"),
        ("POST", "/projects/exact-canary/procurement/dashboard"),
        ("POST", "/projects/exact-canary/supply-chain/seed"),
        ("POST", "/projects/exact-canary/supply-chain/import"),
        ("GET", "/projects/exact-canary/supply-chain/assessments"),
        ("POST", "/projects/exact-canary/supply-chain/shipments/imported-1/assessment"),
        ("GET", "/projects/exact-canary/supply-chain/alerts"),
        ("GET", "/projects/exact-canary/supply-chain/shipments/imported-1/timeline"),
        ("POST", "/projects/exact-canary/demo/reset"),
        ("POST", "/projects/exact-canary/demo/vertical-scenario"),
        ("GET", "/projects/exact-canary/executive-summary"),
        ("GET", "/projects/exact-canary/supply-chain/shipments"),
        ("POST", "/projects/exact-canary/supply-chain/shipments/shipment-1/risk-events"),
        ("GET", "/projects/exact-canary/supply-chain/shipments/shipment-1/risk"),
        ("GET", "/projects/exact-canary/supply-chain/shipments/shipment-1/alternatives"),
        ("GET", "/projects/exact-canary/equipment/SWGR-A/digital-thread"),
        ("POST", "/projects/exact-canary/equipment/SWGR-A/impact-chain/events"),
        ("GET", "/projects/exact-canary/equipment/SWGR-A/impact-chain"),
        ("POST", "/projects/exact-canary/impact-chains"),
        ("POST", "/projects/exact-canary/impact-chains/chain-1/decision"),
        ("POST", "/api/evaluation/run"),
        ("GET", "/api/evaluation/runs/evaluation-1"),
        ("POST", "/api/mitigations/simulate"),
        ("POST", "/api/mitigations/simulation-1/select"),
        ("POST", "/api/benchmarks"),
        ("GET", "/api/benchmarks/summary"),
    }
    assert expected <= set(calls)


def test_execute_acceptance_preserves_failures_and_writes_one_combined_report(tmp_path) -> None:
    class ControlledRunner:
        api_url = API_URL
        frontend_url = FRONTEND_URL
        project_name = CANARY_NAME

        def run_foundation(self):
            return [AcceptanceStep("readiness", AcceptanceStatus.FAIL, 503, 4, {"reason": "degraded"})], "project-1"

        def ensure_documents(self, project_id, sources):
            assert project_id == "project-1"
            return [AcceptanceStep("document:synthetic.md", AcceptanceStatus.PASS, 201, 7, {})], [
                {"id": "document-1", "status": "completed"}
            ]

        def run_feature_manifest(self, project_id, documents):
            assert project_id == "project-1"
            assert documents[0]["id"] == "document-1"
            return [AcceptanceStep("copilot_grounding", AcceptanceStatus.FAIL, 200, 9, {"reason": "missing_citations"})]

    json_path, markdown_path, report = execute_acceptance(
        ControlledRunner(),
        deployed_sha="deadbeef",
        output=tmp_path / "combined.json",
        sources=[("specification", tmp_path / "synthetic.md")],
    )

    assert json_path.exists() and markdown_path.exists()
    assert [step.name for step in report.steps] == ["readiness", "document:synthetic.md", "copilot_grounding"]
    assert json.loads(json_path.read_text(encoding="utf-8"))["summary"] == {"FAIL": 2, "PASS": 1}


def test_shared_client_blocks_direct_mutation_without_permission() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"id": "must-not-exist"})

    acceptance, client = runner(handler, allow_synthetic_mutations=False)
    with client:
        step, body = acceptance.json_step(
            "direct_mutation",
            "POST",
            "/projects/project-1/demo/reset",
            validate=lambda value: True,
            failure_reason="unused",
        )

    assert body is None
    assert step.status is AcceptanceStatus.BLOCKED
    assert step.detail == {"reason": "synthetic_mutations_not_authorized"}
    assert requests == []


def test_semantic_validator_exception_becomes_failure_instead_of_aborting_manifest() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    acceptance, client = runner(handler)
    with client:
        step, body = acceptance.json_step(
            "malformed_success",
            "GET",
            "/projects/project-1/graph",
            validate=lambda value: value["nodes"][0]["id"] == "SWGR-A",
            failure_reason="graph_contract_failed",
        )

    assert body == {"unexpected": "shape"}
    assert step.status is AcceptanceStatus.FAIL
    assert step.detail == {"reason": "semantic_validation_error", "error_type": "KeyError"}
