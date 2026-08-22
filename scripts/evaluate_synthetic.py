"""Run Project Atlas synthetic end-to-end evaluation without external services."""

import asyncio
import json
import logging
import tempfile
import time
import warnings
from pathlib import Path
from statistics import fmean
from typing import Any

warnings.filterwarnings("ignore", message=r"Using `httpx` with `starlette\.testclient` is deprecated.*", category=UserWarning)

from fastapi.testclient import TestClient
from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.commissioning import CommissioningService
from app.compliance import ComplianceService
from app.config import Settings
from app.graph import GraphStore
from app.ingestion import IngestionError
from app.main import app
from app.models import Base
from app.schedule import ScheduleService
from app.workflow import AnswerCitation, AnswerClaim, AnswerResult, ConversationMessage, KnowledgeService, SupportingSpan

ROOT = Path(__file__).parents[1]
DATASET = ROOT / "data" / "synthetic_epc"
RFI_K = 5


class SyntheticEmbedder:
    dimensions = 8
    terms = ("ups", "switchgear", "clearance", "delivery", "battery", "autonomy", "louvre", "crac")

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[float(term in text.lower()) for term in self.terms] for text in texts]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return [[float(term in text.lower()) for term in self.terms] for text in texts]


class SyntheticResponder:
    async def rewrite(self, question: str, history: list[ConversationMessage]) -> str:
        return question

    async def answer(self, question: str, context) -> AnswerResult:
        preferred = next(
            (item for item in context.chunks if "not less than 15 minutes" in item.text), context.chunks[0]
        )
        return AnswerResult(
            answer=f"{preferred.text} [C1]",
            citations=[
                AnswerCitation(
                    **preferred.citation.model_dump(),
                    citation_id="C1",
                    chunk_id=preferred.chunk_id,
                    supporting_spans=[SupportingSpan(text=preferred.text, start=0, end=len(preferred.text))],
                )
            ],
            claims=[AnswerClaim(text=preferred.text, type="fact", citation_ids=["C1"])],
            confidence=1,
            status="ANSWERED",
        )


def synthetic_sources() -> list[tuple[str, Path]]:
    return [
        *(("specification", path) for path in sorted((DATASET / "specifications").glob("*.md"))),
        *(("submittal", path) for path in sorted((DATASET / "submittals").glob("*.md"))),
        *(("RFI", path) for path in sorted((DATASET / "rfis").glob("*.md"))),
        ("meeting_minutes", DATASET / "meeting_minutes" / "MM-014_delivery_risk_review.md"),
        ("change_order", DATASET / "change_orders" / "CO-001_switchgear_recovery.md"),
        ("schedule", DATASET / "schedules" / "atlas_demo_schedule.csv"),
        *(("commissioning_record", path) for path in sorted((DATASET / "commissioning").glob("*.md"))),
    ]


def run_evaluation() -> dict[str, Any]:
    latencies: list[float] = []
    previous_log_level = logging.root.manager.disable
    logging.disable(logging.INFO)
    with tempfile.TemporaryDirectory(prefix="atlas-eval-") as directory:
        workspace = Path(directory)
        settings = Settings(
            log_level="WARNING",
            embedding_dimensions=8,
            qdrant_collection="atlas_synthetic_evaluation",
            upload_dir=str(workspace / "uploads"),
            graph_dir=str(workspace / "graphs"),
        )
        engine = create_async_engine(f"sqlite+aiosqlite:///{workspace / 'atlas.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        qdrant = AsyncQdrantClient(location=":memory:", check_compatibility=False)

        async def create_schema() -> None:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

        asyncio.run(create_schema())
        try:
            with TestClient(app) as client:
                app.state.settings = settings
                app.state.session_factory = sessions
                app.state.qdrant = qdrant
                app.state.embedder = SyntheticEmbedder()
                app.state.graph_store = GraphStore(settings.graph_dir)
                app.state.knowledge_service = KnowledgeService(settings, qdrant, app.state.embedder, SyntheticResponder())
                app.state.compliance_service = ComplianceService(settings)
                app.state.schedule_service = ScheduleService(settings)
                app.state.commissioning_service = CommissioningService(settings)

                def call(method: str, path: str, *, expected: int = 200, **kwargs: Any) -> Any:
                    started = time.perf_counter()
                    response = getattr(client, method)(path, **kwargs)
                    latencies.append((time.perf_counter() - started) * 1_000)
                    if response.status_code != expected:
                        raise AssertionError(f"{method.upper()} {path}: {response.status_code} {response.text}")
                    return response.json()

                project = call("post", "/projects", expected=201, json={"name": "Synthetic E2E evaluation"})
                project_id = project["id"]
                documents: dict[str, dict[str, Any]] = {}
                for document_type, source in synthetic_sources():
                    content_type = "text/csv" if source.suffix == ".csv" else "text/markdown"
                    uploaded = call(
                        "post",
                        f"/projects/{project_id}/documents",
                        expected=201,
                        data={"document_type": document_type},
                        files={"file": (source.name, source.read_bytes(), content_type)},
                    )
                    if uploaded["document"]["status"] != "completed" or uploaded["ingestion"]["status"] != "completed":
                        raise AssertionError(f"Ingestion did not complete for {source.name}")
                    documents[source.name] = uploaded["document"]
                for document in documents.values():
                    status = call("get", f"/projects/{project_id}/documents/{document['id']}/ingestion")
                    if status["ingestion"]["status"] != "completed":
                        raise AssertionError(f"Ingestion status did not persist for {document['filename']}")

                copilot = call(
                    "post",
                    f"/projects/{project_id}/copilot",
                    json={"question": "What is the minimum UPS-A battery autonomy?", "history": []},
                )
                if "15 minutes" not in copilot["answer"]:
                    raise AssertionError("Copilot did not return the planted UPS autonomy evidence")

                truth = json.loads((DATASET / "ground_truth.json").read_text())
                rfi_ranks: dict[str, int] = {}
                rfi_citations: list[dict[str, Any]] = []
                for expected in truth["expected_duplicate_rfi_matches"]:
                    proposed = (DATASET / expected["new_rfi"]).read_text()
                    result = call("post", f"/projects/{project_id}/rfis/matches", json={"proposed_rfi": proposed})
                    target = Path(expected["matching_answered_rfi"]).name
                    ranking = next((index + 1 for index, item in enumerate(result["matches"]) if item["citation"]["filename"] == target), 0)
                    if not ranking or expected["expected_answer"] not in result["matches"][ranking - 1]["previous_answer"]:
                        raise AssertionError(f"Expected RFI resolution was not retrieved for {Path(expected['new_rfi']).name}")
                    rfi_ranks[Path(expected["new_rfi"]).name] = ranking
                    rfi_citations.append(result["matches"][ranking - 1]["citation"])

                findings: list[dict[str, Any]] = []
                for specification, submittals in {
                    "UPS_Specification.md": ("UPS-001_ApexPower_UPS-A.md", "UPS-002_VoltEdge_UPS-A.md"),
                    "CRAC_Specification.md": ("CRAC-001_PolarAir_CRAC-1.md", "CRAC-002_ThermalCore_CRAC-1.md"),
                    "Switchgear_Specification.md": ("SWGR-001_GridPoint_SWGR-A.md", "SWGR-002_ArcLine_SWGR-A.md"),
                }.items():
                    for submittal in submittals:
                        result = call(
                            "post",
                            f"/projects/{project_id}/compliance/checks",
                            json={"specification_document_id": documents[specification]["id"], "submittal_document_id": documents[submittal]["id"]},
                        )
                        findings.extend(result["findings"])
                compliance = call("get", f"/projects/{project_id}/compliance/evaluation")
                flawed = next(item for item in findings if item["status"] == "NON_COMPLIANT")
                reviewed = call("patch", f"/projects/{project_id}/compliance/findings/{flawed['id']}/review", json={"decision": "approved"})
                if reviewed["review_status"] != "approved":
                    raise AssertionError("Compliance approval did not persist")

                schedule = call(
                    "post",
                    f"/projects/{project_id}/schedule/analysis",
                    json={"schedule_document_id": documents["atlas_demo_schedule.csv"]["id"], "analysis_date": "2026-04-15"},
                )
                delivery_risk = next(item for item in schedule["risks"] if item["affected_task"] == "T-140")
                critical_successor = next(item for item in schedule["risks"] if item["affected_task"] == "T-180")
                if not all(any(step.startswith(task) for step in critical_successor["dependency_chain"]) for task in ("T-140", "T-160", "T-170", "T-180")):
                    raise AssertionError("Schedule critical delay chain is incomplete")

                procedure = call("get", f"/projects/{project_id}/commissioning/procedures/{documents['UPS_Procedure_Template.md']['id']}")
                record = call(
                    "post",
                    f"/projects/{project_id}/commissioning/records",
                    expected=201,
                    json={"procedure_document_id": procedure["document_id"], "observations": [{"step_index": step["index"], "observation": "Verified complete."} for step in procedure["steps"]]},
                )
                if record["status"] != "pass" or record["coverage_percent"] != 100:
                    raise AssertionError("Commissioning procedure did not create a complete passing record")

                citation_checks = [
                    any(item["filename"] == "UPS_Specification.md" and item["page"] == 2 for item in copilot["citations"]),
                    *(item["page"] == 1 for item in rfi_citations),
                    *(item["specification_citation"]["page"] == 2 and item["submittal_citation"]["page"] == 1 for item in findings),
                    all(item["filename"] == "atlas_demo_schedule.csv" and item["page"] == 1 for item in delivery_risk["evidence"]),
                    all(step["citation"]["filename"] == "UPS_Procedure_Template.md" for step in record["steps"]),
                ]
                result = {
                    "ingestion": {"uploaded_documents": len(documents), "completed_documents": len(documents)},
                    "compliance": compliance,
                    "rfi": {"k": RFI_K, "recall_at_k": sum(rank <= RFI_K for rank in rfi_ranks.values()) / len(rfi_ranks), "expected_pair_ranks": rfi_ranks},
                    "citation_correctness": {"correct": sum(citation_checks), "checked": len(citation_checks), "rate": sum(citation_checks) / len(citation_checks)},
                    "schedule": {"risk_task": delivery_risk["affected_task"], "risk_lead_time_days": delivery_risk["risk_lead_time_days"], "critical_chain": critical_successor["dependency_chain"]},
                    "commissioning": {"coverage_percent": record["coverage_percent"], "status": record["status"]},
                    "average_response_latency_ms": round(fmean(latencies), 2),
                }
                return result
        finally:
            asyncio.run(engine.dispose())
            logging.disable(previous_log_level)


if __name__ == "__main__":
    print(json.dumps(run_evaluation(), indent=2, sort_keys=True))
