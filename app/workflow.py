import asyncio
import json
import re
import uuid
from time import perf_counter
from typing import Awaitable, Callable, Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.context import ContextBundle, ContextChunk, PostRetrievalProcessor, RevisionConflict
from app.guardrails import reject_prompt_injection
from app.ingestion import (
    Citation,
    DocumentType,
    IngestionError,
    RetrievalResult,
    entity_references,
    retrieve_chunks,
    retrieve_parent_chunks,
)
from app.llm import GeminiGateway


class WorkflowState(TypedDict):
    project_id: str
    status: str


def build_workflow():
    graph = StateGraph(WorkflowState)
    graph.add_node("ready", lambda state: {**state, "status": "ready"})
    graph.add_edge(START, "ready")
    graph.add_edge("ready", END)
    return graph.compile()


class ConversationMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=4_000)


QueryIntent = Literal[
    "knowledge_query",
    "rfi_search",
    "compliance_query",
    "schedule_query",
    "commissioning_query",
    "procurement_query",
]


class QueryPlan(BaseModel):
    original_query: str
    standalone_query: str
    intent: QueryIntent
    project_id: uuid.UUID
    document_types: list[DocumentType] = Field(default_factory=list)
    document_ids: list[uuid.UUID] = Field(default_factory=list)
    equipment_ids: list[str] = Field(default_factory=list)
    vendor_ids: list[str] = Field(default_factory=list)
    revision_status: str | None = None
    section: str | None = None
    subqueries: list[str] = Field(default_factory=list, max_length=3)


class QueryPlanResult(BaseModel):
    plan: QueryPlan
    service: str
    endpoint: str


class Planner(Protocol):
    async def plan(self, project_id: uuid.UUID, query: str, history: list[ConversationMessage]) -> QueryPlan: ...


class GeminiQueryPlanner:
    def __init__(self, settings: Settings, gateway: GeminiGateway | None = None) -> None:
        self.gateway = gateway or GeminiGateway(settings)

    async def plan(self, project_id: uuid.UUID, query: str, history: list[ConversationMessage]) -> QueryPlan:
        fallback = _local_query_plan(project_id, query, history)
        if not getattr(self.gateway, "is_available", bool(getattr(self.gateway, "client", None))):
            return fallback
        context = _planner_context(history)
        try:
            raw = await self.gateway.generate(
                "Return one JSON object matching the requested schema. Rewrite the query using only the supplied conversation context. Select one intent. Extract filters only when explicitly stated. Never invent document, equipment, vendor, or project IDs.",
                json.dumps(
                    {
                        "project_id": str(project_id),
                        "conversation_summary_and_latest_messages": context,
                        "latest_query": query,
                        "schema": QueryPlan.model_json_schema(),
                    }
                ),
                json_output=True,
            )
            plan = QueryPlan.model_validate_json(raw)
        except (ValidationError, ValueError, json.JSONDecodeError):
            return fallback
        return _sanitize_query_plan(plan, project_id, query, history, fallback)


def _planner_context(history: list[ConversationMessage]) -> dict[str, object]:
    earlier, recent = history[:-4], history[-4:]
    summary = " ".join(message.content for message in earlier[-2:])[:500]
    return {
        "summary": summary,
        "latest_messages": [message.model_dump() for message in recent],
    }


def _local_query_plan(project_id: uuid.UUID, query: str, history: list[ConversationMessage]) -> QueryPlan:
    recent_user = next((message.content for message in reversed(history) if message.role == "user"), "")
    follow_up = bool(re.match(r"^(?:and|what about|how about|does it|that|those)\b", query.strip(), re.IGNORECASE))
    standalone = f"{recent_user} {query}".strip() if follow_up and recent_user else query
    lower = standalone.lower()
    intent: QueryIntent = "knowledge_query"
    if "rfi" in lower:
        intent = "rfi_search"
    elif any(term in lower for term in ("compliance", "submittal", "deviation", "non-compliance")):
        intent = "compliance_query"
    elif any(term in lower for term in ("schedule", "critical path", "float", "delay")):
        intent = "schedule_query"
    elif any(term in lower for term in ("commissioning", "test procedure", "acceptance criteria")):
        intent = "commissioning_query"
    elif any(term in lower for term in ("procurement", "shipment", "supplier tracking")):
        intent = "procurement_query"
    document_types = ["RFI"] if intent == "rfi_search" else []
    equipment_ids = entity_references(_context_text(query, history))["equipment_tags"]
    return QueryPlan(
        original_query=query,
        standalone_query=standalone,
        intent=intent,
        project_id=project_id,
        document_types=document_types,
        equipment_ids=equipment_ids,
        subqueries=_split_subqueries(standalone),
    )


def _sanitize_query_plan(
    plan: QueryPlan,
    project_id: uuid.UUID,
    query: str,
    history: list[ConversationMessage],
    fallback: QueryPlan,
) -> QueryPlan:
    context = _context_text(query, history)
    allowed_document_ids = {uuid.UUID(value) for value in re.findall(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", context)}
    allowed_equipment = set(entity_references(context)["equipment_tags"])
    vendor_ids = [value for value in plan.vendor_ids if re.search(rf"\b{re.escape(value)}\b", context, re.IGNORECASE)]
    revision_status = plan.revision_status if plan.revision_status and plan.revision_status.lower() in context.lower() else None
    section = plan.section if plan.section and plan.section.lower() in context.lower() else None
    standalone = plan.standalone_query.strip() or fallback.standalone_query
    return plan.model_copy(
        update={
            "original_query": query,
            "standalone_query": standalone,
            "project_id": project_id,
            "document_ids": [value for value in plan.document_ids if value in allowed_document_ids],
            "equipment_ids": [value for value in plan.equipment_ids if value in allowed_equipment],
            "vendor_ids": vendor_ids,
            "revision_status": revision_status,
            "section": section,
            "subqueries": _validated_subqueries(standalone, plan.subqueries),
        }
    )


def _validated_subqueries(query: str, proposed: list[str]) -> list[str]:
    if not _is_multi_part(query):
        return []
    values = list(dict.fromkeys(value.strip() for value in proposed if value.strip()))[:3]
    return values if len(values) > 1 else _split_subqueries(query)


def _split_subqueries(query: str) -> list[str]:
    parts = [part.strip(" ,?.") for part in re.split(r"\s+(?:and|also)\s+|[;?]+", query, flags=re.IGNORECASE)]
    parts = [part for part in parts if len(part.split()) >= 2]
    return list(dict.fromkeys(parts))[:3] if len(parts) > 1 else []


def _is_multi_part(query: str) -> bool:
    return len(_split_subqueries(query)) > 1


def _context_text(query: str, history: list[ConversationMessage]) -> str:
    return "\n".join([query, *(message.content for message in history)])


AnswerStatus = Literal["ANSWERED", "PARTIAL", "CONFLICTING_EVIDENCE", "INSUFFICIENT_EVIDENCE"]
ClaimType = Literal["fact", "calculation", "recommendation"]
ClaimSupport = Literal["SUPPORTED", "PARTIAL", "UNSUPPORTED"]


class SupportingSpan(BaseModel):
    text: str
    start: int
    end: int


class AnswerCitation(Citation):
    citation_id: str
    chunk_id: str
    supporting_spans: list[SupportingSpan] = Field(min_length=1)


class AnswerClaim(BaseModel):
    text: str = Field(min_length=1)
    type: ClaimType
    citation_ids: list[str] = Field(default_factory=list)
    support_status: ClaimSupport = "SUPPORTED"


class AnswerResult(BaseModel):
    answer: str
    citations: list[AnswerCitation] = Field(default_factory=list)
    claims: list[AnswerClaim] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    status: AnswerStatus
    missing_information: list[str] = Field(default_factory=list)
    conflicting_sources: list[RevisionConflict] = Field(default_factory=list)


class CopilotResult(AnswerResult):
    rewritten_question: str
    trace: "WorkflowTrace" = Field(default_factory=lambda: WorkflowTrace())


class WorkflowTrace(BaseModel):
    stage_latency_ms: dict[str, float] = Field(default_factory=dict)
    candidate_chunk_ids: list[str] = Field(default_factory=list)
    selected_chunk_ids: list[str] = Field(default_factory=list)
    context_tokens: int = 0
    retry_count: int = 0
    final_status: AnswerStatus | None = None


class _GeneratedClaim(BaseModel):
    text: str = Field(min_length=1)
    type: ClaimType
    citation_ids: list[str] = Field(default_factory=list)


class _GeneratedAnswer(BaseModel):
    answer: str
    citation_ids: list[str] = Field(default_factory=list)
    claims: list[_GeneratedClaim] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    status: AnswerStatus
    missing_information: list[str] = Field(default_factory=list)


class _VerificationDecision(BaseModel):
    claim_index: int
    status: ClaimSupport


class _VerificationResult(BaseModel):
    decisions: list[_VerificationDecision]


class RfiMatch(BaseModel):
    label: str = "possible previous match"
    similarity_score: float
    shared_equipment: list[str]
    shared_specification_references: list[str]
    previous_answer: str
    citation: Citation


class RfiResult(BaseModel):
    matches: list[RfiMatch]


class Responder(Protocol):
    async def rewrite(self, question: str, history: list[ConversationMessage]) -> str: ...

    async def answer(self, question: str, context: ContextBundle) -> AnswerResult: ...


class GeminiResponder:
    def __init__(self, settings: Settings, gateway: GeminiGateway | None = None) -> None:
        self.gateway = gateway or GeminiGateway(settings)

    async def rewrite(self, question: str, history: list[ConversationMessage]) -> str:
        if not history:
            return question
        if not getattr(self.gateway, "is_available", bool(getattr(self.gateway, "client", None))):
            raise IngestionError("generation_unavailable", "ATLAS_GROQ_API_KEY or ATLAS_GEMINI_API_KEY is required for knowledge responses", 503)
        context = "\n".join(f"{message.role}: {message.content}" for message in history[-6:])
        return await self._complete(
            "Rewrite the latest user question so it is standalone. Preserve intent and do not answer it.",
            f"Conversation:\n{context}\n\nLatest question: {question}",
        )

    async def answer(self, question: str, context: ContextBundle) -> AnswerResult:
        generated = await self.generate(question, context)
        return await self.verify(generated, context)

    async def generate(self, question: str, context: ContextBundle) -> _GeneratedAnswer:
        if not context.chunks:
            return _GeneratedAnswer(
                answer="Insufficient evidence in this project.",
                confidence=0,
                status="INSUFFICIENT_EVIDENCE",
            )
        if not getattr(self.gateway, "is_available", bool(getattr(self.gateway, "client", None))):
            raise IngestionError("generation_unavailable", "ATLAS_GROQ_API_KEY or ATLAS_GEMINI_API_KEY is required for knowledge responses", 503)
        citation_map = {f"C{index}": chunk for index, chunk in enumerate(context.chunks, start=1)}
        evidence = [
            {
                "citation_id": citation_id,
                "document": chunk.citation.filename,
                "page": chunk.page,
                "section": chunk.section,
                "chunk_id": chunk.chunk_id,
                "text": chunk.text,
            }
            for citation_id, chunk in citation_map.items()
        ]
        raw = await self.gateway.generate(
            "Return JSON only. Answer solely from EVIDENCE. Cite each factual claim inline as [C1]. Classify every claim as fact, calculation, or recommendation. Use only supplied citation IDs. State conflicts and missing information; use INSUFFICIENT_EVIDENCE rather than guessing.",
            json.dumps(
                {
                    "question": question,
                    "evidence": evidence,
                    "revision_conflicts": [item.model_dump(mode="json") for item in context.revision_conflicts],
                    "response_schema": _GeneratedAnswer.model_json_schema(),
                }
            ),
            json_output=True,
        )
        try:
            generated = _GeneratedAnswer.model_validate_json(raw)
        except (ValidationError, ValueError) as exc:
            raise IngestionError("invalid_generation", "The generated answer did not match the required schema", 502) from exc
        return generated

    async def verify(self, generated: _GeneratedAnswer, context: ContextBundle) -> AnswerResult:
        citation_map = {f"C{index}": chunk for index, chunk in enumerate(context.chunks, start=1)}
        return await _ground_answer(generated, citation_map, context, self.gateway)

    async def _complete(self, instructions: str, content: str) -> str:
        return await self.gateway.generate(instructions, content)


class KnowledgeState(TypedDict, total=False):
    project_id: str
    question: str
    history: list[ConversationMessage]
    query_plan: QueryPlan
    rewritten_question: str
    route_service: str
    route_endpoint: str
    candidate_batches: list[list[RetrievalResult]]
    previous_evidence: list[RetrievalResult]
    evidence: list[RetrievalResult]
    ranked_items: list[tuple[RetrievalResult, float]]
    revision_conflicts: list[RevisionConflict]
    expanded_items: list[tuple[RetrievalResult, float, str, list[str]]]
    context_bundle: ContextBundle
    generated_answer: object
    answer_result: AnswerResult
    retry_count: int
    corrective_query: str
    stage_latency_ms: dict[str, float]
    candidate_chunk_ids: list[str]
    trace: WorkflowTrace


class RfiState(TypedDict, total=False):
    project_id: str
    proposed_rfi: str
    threshold: float
    query_plan: QueryPlan
    candidates: list[RetrievalResult]
    matches: list[RfiMatch]


def build_knowledge_workflow(service: "KnowledgeService"):
    async def query_plan(state: KnowledgeState) -> dict[str, object]:
        started = perf_counter()
        plan = await service.query_plan(uuid.UUID(state["project_id"]), state["question"], state.get("history", []))
        return {
            "query_plan": plan,
            "rewritten_question": plan.standalone_query,
            "stage_latency_ms": _timing(state, "query_plan", started),
        }

    async def route_intent(state: KnowledgeState) -> dict[str, object]:
        started = perf_counter()
        service_name, endpoint = _route_destination(state["query_plan"])
        return {
            "route_service": service_name,
            "route_endpoint": endpoint,
            "stage_latency_ms": _timing(state, "route_intent", started),
        }

    async def hybrid_retrieve(state: KnowledgeState) -> dict[str, object]:
        started = perf_counter()
        batches = await service._retrieve_batches(
            state["project_id"], state["rewritten_question"], state["query_plan"]
        )
        ids = list(dict.fromkeys([*state.get("candidate_chunk_ids", []), *(item.chunk_id for batch in batches for item in batch)]))
        return {
            "candidate_batches": batches,
            "candidate_chunk_ids": ids,
            "stage_latency_ms": _timing(state, "hybrid_retrieve", started),
        }

    def rrf(state: KnowledgeState) -> dict[str, object]:
        started = perf_counter()
        evidence = _merge_result_batches(state.get("candidate_batches", []), service.settings.hybrid_retrieval_limit)
        if state.get("previous_evidence"):
            evidence = _merge_result_batches([state["previous_evidence"], evidence], service.settings.hybrid_retrieval_limit)
        evidence = [item for item in evidence if item.project_id == uuid.UUID(state["project_id"]) and item.score > 0.05]
        return {"evidence": evidence, "stage_latency_ms": _timing(state, "rrf", started)}

    async def rerank(state: KnowledgeState) -> dict[str, object]:
        started = perf_counter()
        selected, conflicts = await service.postprocessor.rerank(
            state["rewritten_question"],
            uuid.UUID(state["project_id"]),
            state.get("evidence", [])[: service.settings.rerank_candidate_limit],
        )
        return {
            "ranked_items": selected,
            "revision_conflicts": conflicts,
            "stage_latency_ms": _timing(state, "rerank", started),
        }

    async def parent_expand(state: KnowledgeState) -> dict[str, object]:
        started = perf_counter()
        expanded = await service.postprocessor.expand(state.get("ranked_items", []))
        return {"expanded_items": expanded, "stage_latency_ms": _timing(state, "parent_expand", started)}

    def compress(state: KnowledgeState) -> dict[str, object]:
        started = perf_counter()
        context = service.postprocessor.compress(
            state["rewritten_question"],
            uuid.UUID(state["project_id"]),
            state.get("expanded_items", []),
            state.get("revision_conflicts", []),
        )
        return {"context_bundle": context, "stage_latency_ms": _timing(state, "compress", started)}

    def evidence_gate(state: KnowledgeState) -> dict[str, object]:
        started = perf_counter()
        context, plan = state["context_bundle"], state["query_plan"]
        reasons = service._sufficiency(context, plan)
        corrective = _corrective_query(state["rewritten_question"], plan, reasons) if reasons else None
        context = context.model_copy(
            update={
                "sufficient": not reasons,
                "sufficiency_reasons": reasons,
                "retrieval_attempts": state.get("retry_count", 0) + 1,
                "corrective_query": corrective,
            }
        )
        return {
            "context_bundle": context,
            "corrective_query": corrective or "",
            "stage_latency_ms": _timing(state, "evidence_gate", started),
        }

    async def corrective_retrieve(state: KnowledgeState) -> dict[str, object]:
        started = perf_counter()
        retry_count = min(1, state.get("retry_count", 0) + 1)
        retry_plan = state["query_plan"].model_copy(
            update={"standalone_query": state["corrective_query"], "subqueries": []}
        )
        batches = await service._retrieve_batches(state["project_id"], state["corrective_query"], retry_plan)
        ids = list(dict.fromkeys([*state.get("candidate_chunk_ids", []), *(item.chunk_id for batch in batches for item in batch)]))
        return {
            "candidate_batches": batches,
            "previous_evidence": state.get("evidence", []),
            "candidate_chunk_ids": ids,
            "retry_count": retry_count,
            "stage_latency_ms": _timing(state, "corrective_retrieve", started),
        }

    async def generate(state: KnowledgeState) -> dict[str, object]:
        started = perf_counter()
        context = state["context_bundle"]
        generated = (
            _insufficient_answer(context.sufficiency_reasons, context.revision_conflicts)
            if not context.sufficient
            else await service._generate_answer(state["rewritten_question"], context)
        )
        return {"generated_answer": generated, "stage_latency_ms": _timing(state, "generate", started)}

    async def verify_claims(state: KnowledgeState) -> dict[str, object]:
        started = perf_counter()
        answer = await service._verify_answer(state["generated_answer"], state["context_bundle"])
        return {"answer_result": answer, "stage_latency_ms": _timing(state, "verify_claims", started)}

    def finalize(state: KnowledgeState) -> dict[str, object]:
        started = perf_counter()
        timings = _timing(state, "finalize", started)
        context, answer = state["context_bundle"], state["answer_result"]
        trace = WorkflowTrace(
            stage_latency_ms=timings,
            candidate_chunk_ids=state.get("candidate_chunk_ids", []),
            selected_chunk_ids=[item.chunk_id for item in context.chunks],
            context_tokens=context.total_tokens,
            retry_count=state.get("retry_count", 0),
            final_status=answer.status,
        )
        return {"trace": trace, "stage_latency_ms": timings}

    def after_gate(state: KnowledgeState) -> str:
        return "corrective_retrieve" if not state["context_bundle"].sufficient and state.get("retry_count", 0) < 1 else "generate"

    graph = StateGraph(KnowledgeState)
    graph.add_node("query_plan", query_plan)
    graph.add_node("route_intent", route_intent)
    graph.add_node("hybrid_retrieve", hybrid_retrieve)
    graph.add_node("rrf", rrf)
    graph.add_node("rerank", rerank)
    graph.add_node("parent_expand", parent_expand)
    graph.add_node("compress", compress)
    graph.add_node("evidence_gate", evidence_gate)
    graph.add_node("corrective_retrieve", corrective_retrieve)
    graph.add_node("generate", generate)
    graph.add_node("verify_claims", verify_claims)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "query_plan")
    graph.add_edge("query_plan", "route_intent")
    graph.add_edge("route_intent", "hybrid_retrieve")
    graph.add_edge("hybrid_retrieve", "rrf")
    graph.add_edge("rrf", "rerank")
    graph.add_edge("rerank", "parent_expand")
    graph.add_edge("parent_expand", "compress")
    graph.add_edge("compress", "evidence_gate")
    graph.add_conditional_edges("evidence_gate", after_gate, {"corrective_retrieve": "corrective_retrieve", "generate": "generate"})
    graph.add_edge("corrective_retrieve", "rrf")
    graph.add_edge("generate", "verify_claims")
    graph.add_edge("verify_claims", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()


def _timing(state: KnowledgeState, stage: str, started: float) -> dict[str, float]:
    values = dict(state.get("stage_latency_ms", {}))
    values[stage] = round(values.get(stage, 0) + (perf_counter() - started) * 1_000, 3)
    return values


def _route_destination(plan: QueryPlan) -> tuple[str, str]:
    project_id = plan.project_id
    return {
        "knowledge_query": ("knowledge", f"/projects/{project_id}/copilot"),
        "rfi_search": ("rfi", f"/projects/{project_id}/rfis/matches"),
        "compliance_query": ("compliance", f"/projects/{project_id}/compliance/checks"),
        "schedule_query": ("schedule", f"/projects/{project_id}/schedule/analysis"),
        "commissioning_query": (
            "commissioning",
            f"/projects/{project_id}/commissioning/procedures/{{document_id}}",
        ),
        "procurement_query": ("procurement", f"/projects/{project_id}/procurement/dashboard"),
    }[plan.intent]


def build_rfi_workflow(retrieve: Callable[[str, str, QueryPlan], Awaitable[list[RetrievalResult]]]):
    async def gather_candidates(state: RfiState) -> dict[str, list[RetrievalResult]]:
        return {"candidates": await retrieve(state["project_id"], state["proposed_rfi"], state["query_plan"])}

    def rank_candidates(state: RfiState) -> dict[str, list[RfiMatch]]:
        proposed = entity_references(state["proposed_rfi"])
        matches, seen = [], set()
        for candidate in state.get("candidates", []):
            if candidate.citation.document_id in seen or candidate.score < state["threshold"]:
                continue
            previous_answer = _rfi_answer(candidate.text)
            if not previous_answer:
                continue
            seen.add(candidate.citation.document_id)
            attributes = candidate.attributes or entity_references(candidate.text)
            matches.append(
                RfiMatch(
                    similarity_score=round(min(1.0, max(0.0, candidate.score)), 3),
                    shared_equipment=sorted(set(proposed["equipment_tags"]) & set(attributes.get("equipment_tags", []))),
                    shared_specification_references=sorted(
                        set(proposed["spec_references"]) & set(attributes.get("spec_references", []))
                    ),
                    previous_answer=previous_answer,
                    citation=candidate.citation,
                )
            )
        return {"matches": sorted(matches, key=lambda match: match.similarity_score, reverse=True)}

    graph = StateGraph(RfiState)
    graph.add_node("retrieve_answered_rfis", gather_candidates)
    graph.add_node("rank_possible_matches", rank_candidates)
    graph.add_edge(START, "retrieve_answered_rfis")
    graph.add_edge("retrieve_answered_rfis", "rank_possible_matches")
    graph.add_edge("rank_possible_matches", END)
    return graph.compile()


class KnowledgeService:
    def __init__(
        self,
        settings: Settings,
        qdrant,
        embedder,
        responder: Responder | None = None,
        planner: Planner | None = None,
        postprocessor: PostRetrievalProcessor | None = None,
    ) -> None:
        self.settings, self.qdrant, self.embedder = settings, qdrant, embedder
        self.responder = responder or GeminiResponder(settings)
        self.planner = planner or GeminiQueryPlanner(settings)
        self.postprocessor = postprocessor or PostRetrievalProcessor(settings, parent_loader=self._load_parent)
        self.copilot_workflow = build_knowledge_workflow(self)
        self.rfi_workflow = build_rfi_workflow(self._retrieve_answered_rfis)

    async def query_plan(self, project_id: uuid.UUID, query: str, history: list[ConversationMessage]) -> QueryPlan:
        reject_prompt_injection(query)
        return await self.planner.plan(project_id, query, history)

    async def route_query(self, project_id: uuid.UUID, query: str, history: list[ConversationMessage]) -> QueryPlanResult:
        plan = await self.query_plan(project_id, query, history)
        service, endpoint = _route_destination(plan)
        return QueryPlanResult(plan=plan, service=service, endpoint=endpoint)

    async def copilot(self, project_id: uuid.UUID, question: str, history: list[ConversationMessage]) -> CopilotResult:
        state = await self.copilot_workflow.ainvoke(
            {
                "project_id": str(project_id),
                "question": question,
                "history": history,
                "retry_count": 0,
                "stage_latency_ms": {},
                "candidate_chunk_ids": [],
            }
        )
        return CopilotResult(
            **state["answer_result"].model_dump(),
            rewritten_question=state["rewritten_question"],
            trace=state["trace"],
        )

    async def rfi_matches(self, project_id: uuid.UUID, proposed_rfi: str, threshold: float | None = None) -> RfiResult:
        plan = await self.query_plan(project_id, proposed_rfi, [])
        state = await self.rfi_workflow.ainvoke(
            {
                "project_id": str(project_id),
                "proposed_rfi": plan.standalone_query,
                "threshold": threshold if threshold is not None else self.settings.rfi_similarity_threshold,
                "query_plan": plan,
            }
        )
        return RfiResult(matches=state["matches"])

    async def context_bundle(self, project_id: uuid.UUID, query: str) -> ContextBundle:
        plan = await self.query_plan(project_id, query, [])
        results = await self._retrieve_evidence(str(project_id), plan.standalone_query, plan)
        return await self._postprocess(str(project_id), plan.standalone_query, results)

    async def _retrieve_evidence(self, project_id: str, question: str, plan: QueryPlan) -> list[RetrievalResult]:
        batches = await self._retrieve_batches(project_id, question, plan)
        results = _merge_result_batches(batches, self.settings.hybrid_retrieval_limit)
        return [result for result in results if result.score > 0.05]

    async def _retrieve_batches(
        self, project_id: str, question: str, plan: QueryPlan
    ) -> list[list[RetrievalResult]]:
        parsed_project_id = uuid.UUID(project_id)
        if plan.project_id != parsed_project_id:
            raise IngestionError("project_scope_mismatch", "Query plan project does not match the request", 400)
        queries = plan.subqueries[:3] if len(plan.subqueries) > 1 and _is_multi_part(plan.standalone_query) else [question]
        return list(await asyncio.gather(
            *(
                retrieve_chunks(
                    self.qdrant,
                    self.embedder,
                    self.settings,
                    parsed_project_id,
                    query,
                    self.settings.hybrid_retrieval_limit,
                    query_plan=plan,
                )
                for query in queries
            )
        ))

    async def _generate_answer(self, question: str, context: ContextBundle) -> object:
        generate = getattr(self.responder, "generate", None)
        return await generate(question, context) if generate else await self.responder.answer(question, context)

    async def _verify_answer(self, generated: object, context: ContextBundle) -> AnswerResult:
        if isinstance(generated, AnswerResult):
            return generated
        verify = getattr(self.responder, "verify", None)
        if not verify:
            raise IngestionError("generation_verification_unavailable", "Answer verification is unavailable", 503)
        return await verify(generated, context)

    async def _retrieve_answered_rfis(self, project_id: str, question: str, plan: QueryPlan) -> list[RetrievalResult]:
        return await retrieve_chunks(
            self.qdrant, self.embedder, self.settings, uuid.UUID(project_id), question, self.settings.hybrid_retrieval_limit, "RFI", "answered", plan
        )

    async def _postprocess(
        self, project_id: str, question: str, results: list[RetrievalResult]
    ) -> ContextBundle:
        return await self.postprocessor.process(question, uuid.UUID(project_id), results)

    async def _load_parent(self, project_id: uuid.UUID, parent_id: uuid.UUID) -> list[RetrievalResult]:
        return await retrieve_parent_chunks(self.qdrant, self.settings, project_id, parent_id)

    def _sufficiency(self, context: ContextBundle, plan: QueryPlan) -> list[str]:
        return _evidence_sufficiency(context, plan, self.settings)


def _merge_result_batches(batches: list[list[RetrievalResult]], limit: int) -> list[RetrievalResult]:
    if len(batches) == 1:
        return batches[0][:limit]
    fused: dict[str, tuple[RetrievalResult, float]] = {}
    for batch in batches:
        for rank, item in enumerate(batch, start=1):
            previous = fused.get(item.chunk_id)
            fused[item.chunk_id] = (item, (previous[1] if previous else 0) + 1 / (60 + rank))
    denominator = max(len(batches), 1) / 61
    return [
        item.model_copy(update={"rrf_score": score, "score": min(1.0, score / denominator)})
        for item, score in sorted(fused.values(), key=lambda value: (-value[1], value[0].chunk_id))[:limit]
    ]


def _evidence_sufficiency(context: ContextBundle, plan: QueryPlan, settings: Settings) -> list[str]:
    reasons: list[str] = []
    chunks = context.chunks
    if not chunks or not any(_query_terms(context.query) & _query_terms(chunk.text) for chunk in chunks):
        reasons.append("relevant evidence is missing")
    present_types = {chunk.document_type for chunk in chunks}
    missing_types = sorted(set(plan.document_types) - present_types)
    if missing_types:
        reasons.append(f"required document types are missing: {', '.join(missing_types)}")
    if any(chunk.project_id != plan.project_id for chunk in chunks):
        reasons.append("project filter mismatch")
    present_equipment = {
        str(value)
        for chunk in chunks
        for value in (chunk.attributes.get("equipment_ids") or chunk.attributes.get("equipment_tags") or [])
    }
    missing_equipment = sorted(set(plan.equipment_ids) - present_equipment)
    if missing_equipment:
        reasons.append(f"equipment evidence is missing: {', '.join(missing_equipment)}")
    disallowed = [status for status in (_approval_status(chunk) for chunk in chunks) if status and status not in APPROVED_EVIDENCE]
    if disallowed:
        reasons.append(f"non-current revisions found: {', '.join(sorted(set(disallowed)))}")
    if plan.revision_status and not any(_approval_status(chunk) == plan.revision_status.lower() for chunk in chunks):
        reasons.append(f"required revision status is missing: {plan.revision_status}")
    if _requires_value(context.query) and not re.search(r"\b\d+(?:\.\d+)?\b", " ".join(chunk.text for chunk in chunks)):
        reasons.append("answer-bearing values are missing")
    if chunks and max(chunk.rerank_score for chunk in chunks) < settings.reranker_score_threshold:
        reasons.append("reranker score is below threshold")
    return reasons


APPROVED_EVIDENCE = {"approved", "answered", "current", "ifc", "issued for bid", "issued for construction"}
QUERY_STOP_WORDS = {"a", "an", "and", "are", "for", "from", "in", "is", "of", "on", "or", "the", "to", "what"}


def _query_terms(text: str) -> set[str]:
    return {term for term in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", text.lower()) if term not in QUERY_STOP_WORDS}


def _approval_status(chunk: ContextChunk) -> str:
    return str(
        chunk.attributes.get("approval_status")
        or chunk.attributes.get("revision_status")
        or chunk.attributes.get("rfi_status")
        or ""
    ).strip().lower()


def _requires_value(query: str) -> bool:
    return bool(re.search(r"\b(?:autonomy|capacity|clearance|date|duration|frequency|lead time|rating|voltage)\b", query, re.IGNORECASE))


def _corrective_query(query: str, plan: QueryPlan, reasons: list[str]) -> str:
    filters = [
        *(f"document type {value}" for value in plan.document_types),
        *(f"equipment {value}" for value in plan.equipment_ids),
        *(f"vendor {value}" for value in plan.vendor_ids),
    ]
    if any("value" in reason for reason in reasons):
        filters.append("required value acceptance criterion")
    if any("revision" in reason for reason in reasons):
        filters.append("current approved revision")
    return " ".join([query, *filters]).strip()


async def _ground_answer(
    generated: _GeneratedAnswer,
    citation_map: dict[str, ContextChunk],
    context: ContextBundle,
    gateway: GeminiGateway,
) -> AnswerResult:
    known = set(citation_map)
    used = set(generated.citation_ids)
    inline = set(re.findall(r"\[(C\d+)\]", generated.answer))
    for claim in generated.claims:
        used.update(claim.citation_ids)
    if not used <= known or not inline <= known:
        raise IngestionError("invalid_citation", "The generated answer referenced an unknown citation ID", 502)
    if generated.status == "INSUFFICIENT_EVIDENCE":
        return _insufficient_answer(generated.missing_information, context.revision_conflicts)
    if not generated.claims:
        return _insufficient_answer(["No verifiable claims were generated."], context.revision_conflicts)

    statuses = [_deterministic_support(claim, citation_map) for claim in generated.claims]
    uncertain = [index for index, status in enumerate(statuses) if status is None]
    if uncertain:
        verified = await _semantic_verify(gateway, generated.claims, uncertain, citation_map)
        for index in uncertain:
            statuses[index] = verified.get(index, "UNSUPPORTED")
    claims = [
        AnswerClaim(**claim.model_dump(), support_status=status)
        for claim, status in zip(generated.claims, statuses, strict=True)
        if status != "UNSUPPORTED"
    ]
    if not claims:
        return _insufficient_answer(
            [*generated.missing_information, "Generated claims were not supported by project evidence."],
            context.revision_conflicts,
        )
    used = {citation_id for claim in claims for citation_id in claim.citation_ids}
    citations = [
        AnswerCitation(
            **citation_map[citation_id].citation.model_dump(),
            citation_id=citation_id,
            chunk_id=citation_map[citation_id].chunk_id,
            supporting_spans=_supporting_spans(citation_map[citation_id], claims, citation_id),
        )
        for citation_id in citation_map
        if citation_id in used
    ]
    labels = {"fact": "Document fact", "calculation": "Calculation", "recommendation": "Recommendation"}
    answer = "\n".join(
        f"{labels[claim.type]}: {claim.text} {' '.join(f'[{value}]' for value in claim.citation_ids)}".rstrip()
        for claim in claims
    )
    if context.revision_conflicts:
        answer = f"Conflicting revisions were found.\n{answer}"
    removed = len(generated.claims) - len(claims)
    missing = list(generated.missing_information)
    if removed:
        missing.append("One or more unsupported generated claims were removed.")
    status: AnswerStatus = "CONFLICTING_EVIDENCE" if context.revision_conflicts else "ANSWERED"
    if not context.revision_conflicts and (missing or any(claim.support_status == "PARTIAL" for claim in claims)):
        status = "PARTIAL"
    support_ratio = sum(1 if claim.support_status == "SUPPORTED" else 0.5 for claim in claims) / len(generated.claims)
    return AnswerResult(
        answer=answer,
        citations=citations,
        claims=claims,
        confidence=round(generated.confidence * support_ratio, 3),
        status=status,
        missing_information=missing,
        conflicting_sources=context.revision_conflicts,
    )


def _deterministic_support(
    claim: _GeneratedClaim, citation_map: dict[str, ContextChunk]
) -> ClaimSupport | None:
    if not claim.citation_ids:
        return "UNSUPPORTED"
    evidence = " ".join(citation_map[value].text for value in claim.citation_ids)
    exact = _exact_terms(claim.text)
    missing_exact = {value for value in exact if not _contains_exact(evidence, value)}
    if missing_exact and claim.type != "calculation":
        return "UNSUPPORTED"
    claim_terms, evidence_terms = _query_terms(claim.text), _query_terms(evidence)
    overlap = len(claim_terms & evidence_terms) / max(len(claim_terms), 1)
    if claim.type == "calculation":
        return None
    if overlap >= 0.45 or (exact and overlap >= 0.25):
        return "SUPPORTED"
    return None if overlap >= 0.15 else "UNSUPPORTED"


async def _semantic_verify(
    gateway: GeminiGateway,
    claims: list[_GeneratedClaim],
    uncertain: list[int],
    citation_map: dict[str, ContextChunk],
) -> dict[int, ClaimSupport]:
    payload = [
        {
            "claim_index": index,
            "claim": claims[index].model_dump(),
            "evidence": [citation_map[value].text for value in claims[index].citation_ids],
        }
        for index in uncertain
    ]
    try:
        raw = await gateway.generate(
            "Return JSON only. Using only the supplied evidence, mark each claim SUPPORTED, PARTIAL, or UNSUPPORTED. Do not use outside knowledge.",
            json.dumps({"claims": payload, "schema": _VerificationResult.model_json_schema()}),
            json_output=True,
        )
        result = _VerificationResult.model_validate_json(raw)
    except (IngestionError, ValidationError, ValueError):
        return {}
    allowed = set(uncertain)
    return {item.claim_index: item.status for item in result.decisions if item.claim_index in allowed}


def _supporting_spans(chunk: ContextChunk, claims: list[AnswerClaim], citation_id: str) -> list[SupportingSpan]:
    candidates = chunk.evidence_spans or _fallback_spans(chunk.text)
    spans: list[SupportingSpan] = []
    for claim in (item for item in claims if citation_id in item.citation_ids):
        claim_terms = _query_terms(claim.text)
        exact = _exact_terms(claim.text)
        viable = [span for span in candidates if all(_contains_exact(span.text, value) for value in exact)] or candidates
        best = max(viable, key=lambda span: len(claim_terms & _query_terms(span.text)))
        value = SupportingSpan(text=best.text, start=best.start, end=best.end)
        if value not in spans:
            spans.append(value)
    return spans


def _fallback_spans(text: str) -> list[SupportingSpan]:
    spans = []
    for match in re.finditer(r"[^\n.!?]+[.!?]?", text):
        value = match.group().strip()
        if value:
            start = match.start() + len(match.group()) - len(match.group().lstrip())
            spans.append(SupportingSpan(text=value, start=start, end=start + len(value)))
    return spans or [SupportingSpan(text=text, start=0, end=len(text))]


def _exact_terms(text: str) -> set[str]:
    values = re.findall(
        r"\b\d+(?:\.\d+)?\s*(?:%|percent|mm|cm|m|kva|kw|ka|v|hz|minutes?|hours?|days?|°c)\b",
        text,
        re.IGNORECASE,
    )
    identifiers = re.findall(
        r"\b(?:[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+|[A-Z]{2,}\d+[A-Z0-9-]*|\d+(?:\.\d+)*)\b",
        text,
    )
    return {value.strip() for value in [*values, *identifiers]}


def _contains_exact(evidence: str, value: str) -> bool:
    boundary = r"[\w.]" if re.fullmatch(r"\d+(?:\.\d+)*", value) else r"\w"
    return bool(re.search(rf"(?<!{boundary}){re.escape(value)}(?!{boundary})", evidence, re.IGNORECASE))


def _insufficient_answer(
    missing_information: list[str] | None = None,
    conflicting_sources: list[RevisionConflict] | None = None,
) -> AnswerResult:
    return AnswerResult(
        answer="Insufficient evidence in this project.",
        confidence=0,
        status="INSUFFICIENT_EVIDENCE",
        missing_information=missing_information or [],
        conflicting_sources=conflicting_sources or [],
    )


def _rfi_answer(text: str) -> str | None:
    answer = re.search(r"\*\*Answer:\*\*\s*(.+?)(?:\n\*\*Reference:|$)", text, re.DOTALL)
    return answer.group(1).strip() if answer else None
