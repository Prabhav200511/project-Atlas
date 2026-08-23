import uuid

import pytest

from app.config import Settings
from app.context import ContextBundle, LexicalReranker, PostRetrievalProcessor
from app.errors import IngestionError
from app.ingestion import Citation, RetrievalResult
from app.workflow import (
    AnswerCitation,
    AnswerClaim,
    AnswerResult,
    ConversationMessage,
    GeminiQueryPlanner,
    KnowledgeService,
    QueryPlan,
    SupportingSpan,
)


class Planner:
    def __init__(self, plan: QueryPlan) -> None:
        self.value = plan
        self.history: list[ConversationMessage] = []

    async def plan(self, project_id, query, history):
        self.history = history
        return self.value.model_copy(update={"original_query": query, "project_id": project_id})


class Responder:
    async def answer(self, question: str, context: ContextBundle) -> AnswerResult:
        chunk = context.chunks[0]
        citation = AnswerCitation(
            **chunk.citation.model_dump(),
            citation_id="C1",
            chunk_id=chunk.chunk_id,
            supporting_spans=[SupportingSpan(text=chunk.text, start=0, end=len(chunk.text))],
        )
        status = "CONFLICTING_EVIDENCE" if context.revision_conflicts else "ANSWERED"
        return AnswerResult(
            answer=f"{chunk.text} [C1]",
            citations=[citation],
            claims=[AnswerClaim(text=chunk.text, type="fact", citation_ids=["C1"])],
            confidence=1,
            status=status,
            conflicting_sources=context.revision_conflicts,
        )


class UnavailableGateway:
    is_available = False


class UnavailableResponder:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, *_args, **_kwargs) -> object:
        self.calls += 1
        raise IngestionError("generation_unavailable", "AI provider unavailable", 503)


def result(
    project_id: uuid.UUID,
    text: str,
    *,
    chunk_id: str,
    filename: str = "UPS_Specification.md",
    revision: str = "A",
    approval_status: str = "approved",
):
    document_id = uuid.uuid4()
    return RetrievalResult(
        chunk_id=chunk_id,
        parent_id=uuid.uuid4(),
        document_id=document_id,
        document_type="specification",
        project_id=project_id,
        page=2,
        section="Battery",
        text=text,
        score=0.9,
        dense_rank=1,
        bm25_rank=1,
        rrf_score=0.03,
        citation=Citation(document_id=document_id, filename=filename, page=2, section="Battery"),
        attributes={"equipment_ids": ["UPS-A"], "approval_status": approval_status, "revision": revision},
    )


def service(project_id: uuid.UUID, plan: QueryPlan, batches: list[list[RetrievalResult]]):
    settings = Settings(
        reranker_score_threshold=0,
        context_min_chunks=1,
        context_max_chunks=8,
    )
    value = KnowledgeService(
        settings,
        None,
        None,
        responder=Responder(),
        planner=Planner(plan),
        postprocessor=PostRetrievalProcessor(settings, reranker=LexicalReranker()),
    )
    calls: list[str] = []

    async def retrieve(request_project: str, query: str, query_plan: QueryPlan):
        assert uuid.UUID(request_project) == project_id == query_plan.project_id
        queries = query_plan.subqueries if len(query_plan.subqueries) > 1 else [query]
        calls.extend(queries)
        return batches if len(queries) > 1 or len(calls) == 1 else batches[-1:]

    value._retrieve_batches = retrieve
    return value, calls


def outage_service(project_id: uuid.UUID, items: list[RetrievalResult]):
    settings = Settings(
        reranker_score_threshold=0,
        context_min_chunks=1,
        context_max_chunks=8,
    )
    responder = UnavailableResponder()
    value = KnowledgeService(
        settings,
        None,
        None,
        responder=responder,
        planner=GeminiQueryPlanner(settings, gateway=UnavailableGateway()),
        postprocessor=PostRetrievalProcessor(settings, reranker=LexicalReranker()),
    )

    async def retrieve(request_project: str, _query: str, query_plan: QueryPlan):
        assert uuid.UUID(request_project) == project_id == query_plan.project_id
        return [items]

    value._retrieve_batches = retrieve
    return value, responder


def plan(project_id: uuid.UUID, query: str, **updates) -> QueryPlan:
    return QueryPlan(
        original_query=query,
        standalone_query=query,
        intent="knowledge_query",
        project_id=project_id,
        equipment_ids=["UPS-A"],
        **updates,
    )


@pytest.mark.asyncio
async def test_direct_query_runs_all_stages_and_records_safe_trace() -> None:
    project_id = uuid.uuid4()
    query = "What is UPS-A autonomy?"
    atlas, _ = service(project_id, plan(project_id, query), [[result(project_id, "UPS-A autonomy is 15 minutes.", chunk_id="direct")]])

    answer = await atlas.copilot(project_id, query, [])

    assert answer.status == "ANSWERED"
    assert answer.trace.candidate_chunk_ids == ["direct"]
    assert answer.trace.selected_chunk_ids == ["direct"]
    assert answer.trace.final_status == answer.status
    assert set(answer.trace.stage_latency_ms) >= {
        "query_plan", "route_intent", "hybrid_retrieve", "rrf", "rerank", "parent_expand",
        "compress", "evidence_gate", "generate", "verify_claims", "finalize",
    }
    assert "15 minutes" not in str(answer.trace.model_dump())


@pytest.mark.asyncio
async def test_follow_up_uses_planned_standalone_query_and_latest_history() -> None:
    project_id = uuid.uuid4()
    query = "And its autonomy?"
    query_plan = plan(project_id, query).model_copy(update={"standalone_query": "What is UPS-A autonomy?"})
    atlas, calls = service(project_id, query_plan, [[result(project_id, "UPS-A autonomy is 15 minutes.", chunk_id="follow")]])
    history = [ConversationMessage(role="user", content="Tell me about UPS-A.")]

    answer = await atlas.copilot(project_id, query, history)

    assert answer.rewritten_question == "What is UPS-A autonomy?"
    assert calls == [answer.rewritten_question]
    assert atlas.planner.history == history


@pytest.mark.asyncio
async def test_multi_part_query_fuses_and_deduplicates_batches() -> None:
    project_id = uuid.uuid4()
    query = "What is UPS-A autonomy and what is UPS-A voltage?"
    common = result(project_id, "UPS-A autonomy is 15 minutes and voltage is 415 V.", chunk_id="common")
    query_plan = plan(project_id, query, subqueries=["UPS-A autonomy", "UPS-A voltage"])
    atlas, calls = service(project_id, query_plan, [[common], [common]])

    answer = await atlas.copilot(project_id, query, [])

    assert calls == query_plan.subqueries
    assert answer.trace.candidate_chunk_ids == ["common"]
    assert answer.trace.selected_chunk_ids == ["common"]


@pytest.mark.asyncio
async def test_conflicting_revisions_are_preserved_to_final_status() -> None:
    project_id = uuid.uuid4()
    query = "What is UPS-A autonomy?"
    items = [
        result(project_id, "UPS-A autonomy is 15 minutes.", chunk_id="rev-a", filename="UPS_Specification_Rev_A.md", revision="A"),
        result(project_id, "UPS-A autonomy is 10 minutes.", chunk_id="rev-b", filename="UPS_Specification_Rev_B.md", revision="B"),
    ]
    atlas, _ = service(project_id, plan(project_id, query), [items])

    answer = await atlas.copilot(project_id, query, [])

    assert answer.status == "CONFLICTING_EVIDENCE"
    assert answer.conflicting_sources


@pytest.mark.asyncio
async def test_insufficient_evidence_retries_once_then_stops() -> None:
    project_id = uuid.uuid4()
    query = "What is UPS-A autonomy?"
    atlas, calls = service(project_id, plan(project_id, query, document_types=["specification"]), [[]])

    answer = await atlas.copilot(project_id, query, [])

    assert answer.status == "INSUFFICIENT_EVIDENCE"
    assert answer.trace.retry_count == 1
    assert len(calls) == 2
    assert "corrective_retrieve" in answer.trace.stage_latency_ms


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "query"),
    [
        ("proposed", "What UPS-A recovery measure is proposed?"),
        ("issued for review", "What UPS-A recovery measure was issued for review?"),
    ],
)
async def test_full_workflow_answers_explicit_non_current_revision_status(status: str, query: str) -> None:
    project_id = uuid.uuid4()
    query_plan = plan(project_id, query, revision_status=status)
    item = result(
        project_id,
        f"The UPS-A {status} recovery measure is to switch vendor.",
        chunk_id=f"status-{status}",
        approval_status=status,
    )
    atlas, _ = service(project_id, query_plan, [[item]])

    answer = await atlas.copilot(project_id, query, [])

    assert answer.status == "ANSWERED"
    assert answer.trace.selected_chunk_ids == [item.chunk_id]
    assert status in answer.answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "query"),
    [
        ("proposed", "What UPS-A recovery measure is proposed?"),
        ("issued for review", "What UPS-A recovery measure was issued for review?"),
    ],
)
async def test_complete_provider_outage_workflow_preserves_explicit_revision_status(
    status: str,
    query: str,
) -> None:
    project_id = uuid.uuid4()
    item = result(
        project_id,
        f"The UPS-A {status} recovery measure is to switch vendor.",
        chunk_id=f"outage-{status}",
        approval_status=status,
    )
    atlas, responder = outage_service(project_id, [item])

    answer = await atlas.copilot(project_id, query, [])

    assert answer.status == "PARTIAL"
    assert answer.trace.selected_chunk_ids == [item.chunk_id]
    assert f"{status.title()} document fact:" in answer.answer
    assert "switch vendor" in answer.answer
    assert responder.calls == 1


@pytest.mark.asyncio
async def test_complete_provider_outage_workflow_rejects_unrequested_non_current_evidence() -> None:
    project_id = uuid.uuid4()
    query = "What is the UPS-A recovery measure?"
    item = result(
        project_id,
        "The UPS-A proposed recovery measure is to switch vendor.",
        chunk_id="outage-unrequested-proposed",
        approval_status="proposed",
    )
    atlas, responder = outage_service(project_id, [item])

    answer = await atlas.copilot(project_id, query, [])

    assert answer.status == "INSUFFICIENT_EVIDENCE"
    assert "switch vendor" not in answer.answer
    assert responder.calls == 0
