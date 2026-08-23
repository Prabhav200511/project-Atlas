import uuid

import pytest

from app.config import Settings
from app.context import ContextBundle, ContextChunk, RevisionConflict
from app.ingestion import Citation, RetrievalResult
from app.workflow import (
    QueryPlan,
    _evidence_sufficiency,
    build_knowledge_workflow,
    KnowledgeService,
)


def result(
    text: str,
    project_id: uuid.UUID,
    *,
    document_type: str = "specification",
    chunk_id: str | None = None,
    equipment: list[str] | None = None,
    approval: str = "approved",
) -> RetrievalResult:
    document_id = uuid.uuid4()
    return RetrievalResult(
        chunk_id=chunk_id or str(uuid.uuid4()),
        parent_id=uuid.uuid4(),
        document_id=document_id,
        document_type=document_type,
        project_id=project_id,
        page=1,
        section="General",
        text=text,
        score=0.9,
        dense_rank=1,
        bm25_rank=1,
        rrf_score=0.03,
        citation=Citation(document_id=document_id, filename=f"{document_type}.md", page=1, section="General"),
        attributes={"equipment_ids": equipment or [], "approval_status": approval},
    )


def bundle(item: RetrievalResult, query: str, *, conflicts: list[RevisionConflict] | None = None) -> ContextBundle:
    return ContextBundle(
        project_id=item.project_id,
        query=query,
        chunks=[ContextChunk(**item.model_dump(), rerank_score=0.9)],
        revision_conflicts=conflicts or [],
        total_tokens=20,
        max_context_tokens=4_000,
    )


@pytest.mark.asyncio
async def test_multi_part_query_reuses_hybrid_retrieval_and_rrf_deduplicates(monkeypatch) -> None:
    project_id = uuid.uuid4()
    common = result("UPS-A and switchgear requirements.", project_id, chunk_id="common", equipment=["UPS-A"])
    autonomy = result("UPS-A autonomy is 15 minutes.", project_id, chunk_id="autonomy", equipment=["UPS-A"])
    rating = result("Switchgear rating is 65 kA.", project_id, chunk_id="rating")
    calls: list[str] = []

    async def retrieve(*args, **kwargs):
        query = args[4]
        calls.append(query)
        return [common, autonomy] if "autonomy" in query else [common, rating]

    monkeypatch.setattr("app.workflow.retrieve_chunks", retrieve)
    plan = QueryPlan(
        original_query="What is UPS-A autonomy and what is switchgear rating?",
        standalone_query="What is UPS-A autonomy and what is switchgear rating?",
        intent="knowledge_query",
        project_id=project_id,
        subqueries=["What is UPS-A autonomy", "what is switchgear rating"],
    )

    results = await KnowledgeService(Settings(), None, None)._retrieve_evidence(str(project_id), plan.standalone_query, plan)

    assert calls == plan.subqueries
    assert [item.chunk_id for item in results] == ["common", "autonomy", "rating"]
    assert len({item.chunk_id for item in results}) == len(results)


@pytest.mark.asyncio
async def test_missing_document_type_retries_once_then_returns_insufficient() -> None:
    project_id = uuid.uuid4()
    item = result("UPS-A autonomy is 15 minutes.", project_id, document_type="submittal", equipment=["UPS-A"])
    calls: list[str] = []

    async def retrieve(_project: str, query: str, _plan: QueryPlan):
        calls.append(query)
        return [[item]]

    class Postprocessor:
        async def rerank(self, query, project, items):
            return [(items[0], 0.9)], []

        async def expand(self, items):
            return [(items[0][0], items[0][1], items[0][0].text, [])]

        def compress(self, query, project, items, conflicts):
            return bundle(item, query)

    class Responder:
        async def rewrite(self, question, history):
            return question

        async def answer(self, question, context):
            raise AssertionError("generation must not run after retry exhaustion")

    settings = Settings(reranker_score_threshold=0.2)
    plan = QueryPlan(
        original_query="What is UPS-A autonomy?",
        standalone_query="What is UPS-A autonomy?",
        intent="knowledge_query",
        project_id=project_id,
        document_types=["specification"],
        equipment_ids=["UPS-A"],
    )

    class Service:
        postprocessor = Postprocessor()

        def __init__(self):
            self.settings = settings

        async def query_plan(self, project, question, history):
            return plan

        _retrieve_batches = staticmethod(retrieve)

        def _sufficiency(self, context, query_plan):
            return _evidence_sufficiency(context, query_plan, settings)

        async def _generate_answer(self, question, context):
            return await Responder().answer(question, context)

        async def _verify_answer(self, answer, context):
            return answer

    state = await build_knowledge_workflow(Service()).ainvoke(
        {"project_id": str(project_id), "question": plan.standalone_query, "history": [], "retry_count": 0}
    )

    assert len(calls) == 2
    assert "document type specification" in calls[1]
    assert state["context_bundle"].retrieval_attempts == 2
    assert state["answer_result"].status == "INSUFFICIENT_EVIDENCE"
    assert "required document types are missing" in state["answer_result"].missing_information[0]


def test_conflicting_sources_remain_identified_when_approved_evidence_is_sufficient() -> None:
    project_id = uuid.uuid4()
    item = result("UPS-A autonomy is 15 minutes.", project_id, equipment=["UPS-A"])
    conflict = RevisionConflict(
        document_key="ups_spec",
        section="battery",
        document_ids=[uuid.uuid4(), uuid.uuid4()],
        revisions=["a", "b"],
    )
    context = bundle(item, "What is UPS-A autonomy?", conflicts=[conflict])
    plan = QueryPlan(
        original_query=context.query,
        standalone_query=context.query,
        intent="knowledge_query",
        project_id=project_id,
        document_types=["specification"],
        equipment_ids=["UPS-A"],
    )

    assert _evidence_sufficiency(context, plan, Settings(reranker_score_threshold=0.2)) == []
    assert context.revision_conflicts == [conflict]


def test_supplemental_proposed_source_does_not_invalidate_primary_current_evidence() -> None:
    project_id = uuid.uuid4()
    current = result(
        "The active schedule risk is a 35-day switchgear delay.",
        project_id,
        document_type="schedule",
        approval="approved",
    )
    proposed = result(
        "A proposed recovery measure has not been approved.",
        project_id,
        document_type="change_order",
        approval="proposed",
    )
    context = ContextBundle(
        project_id=project_id,
        query="What is the active schedule risk and downstream impact?",
        chunks=[
            ContextChunk(**current.model_dump(), rerank_score=0.9),
            ContextChunk(**proposed.model_dump(), rerank_score=0.7),
        ],
        total_tokens=40,
        max_context_tokens=4_000,
    )
    plan = QueryPlan(
        original_query=context.query,
        standalone_query=context.query,
        intent="knowledge_query",
        project_id=project_id,
    )

    assert _evidence_sufficiency(context, plan, Settings(reranker_score_threshold=0.2)) == []


def test_primary_proposed_source_remains_insufficient_without_requested_revision_status() -> None:
    project_id = uuid.uuid4()
    proposed = result(
        "A proposed recovery measure has not been approved.",
        project_id,
        document_type="change_order",
        approval="proposed",
    )
    context = bundle(proposed, "What recovery measure is committed?")
    plan = QueryPlan(
        original_query=context.query,
        standalone_query=context.query,
        intent="knowledge_query",
        project_id=project_id,
    )

    assert "non-current revisions found: proposed" in _evidence_sufficiency(
        context, plan, Settings(reranker_score_threshold=0.2)
    )


@pytest.mark.parametrize(
    ("status", "query"),
    [
        ("proposed", "What recovery measure is proposed?"),
        ("issued for review", "What was issued for review?"),
    ],
)
def test_explicit_revision_status_allows_matching_non_current_primary(status: str, query: str) -> None:
    project_id = uuid.uuid4()
    requested = result(
        f"The {status} recovery measure is to switch vendor.",
        project_id,
        document_type="change_order",
        approval=status,
    )
    context = bundle(requested, query)
    plan = QueryPlan(
        original_query=query,
        standalone_query=query,
        intent="knowledge_query",
        project_id=project_id,
        revision_status=status,
    )

    assert _evidence_sufficiency(context, plan, Settings(reranker_score_threshold=0.2)) == []
