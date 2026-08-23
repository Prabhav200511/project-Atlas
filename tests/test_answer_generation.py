import json
import uuid

import pytest

from app.config import Settings
from app.context import ContextBundle, ContextChunk, EvidenceSpan, RevisionConflict
from app.ingestion import Citation, IngestionError
from app.workflow import GeminiResponder, KnowledgeService


class FakeGateway:
    client = object()

    def __init__(self, response: dict | list[dict]) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.calls: list[tuple[str, str, bool]] = []

    async def generate(self, instructions: str, content: str, *, json_output: bool = False) -> str:
        self.calls.append((instructions, content, json_output))
        return json.dumps(self.responses[min(len(self.calls) - 1, len(self.responses) - 1)])


def context(text: str = "UPS-A battery autonomy shall be 15 minutes.") -> ContextBundle:
    project_id, document_id = uuid.uuid4(), uuid.uuid4()
    chunk = ContextChunk(
        chunk_id="chunk-15",
        parent_id=uuid.uuid4(),
        document_id=document_id,
        document_type="specification",
        project_id=project_id,
        page=2,
        section="2.2 Battery autonomy",
        text=text,
        score=0.9,
        dense_rank=1,
        bm25_rank=1,
        rrf_score=0.03,
        citation=Citation(
            document_id=document_id,
            filename="UPS_Specification.md",
            page=2,
            section="2.2 Battery autonomy",
        ),
        rerank_score=0.95,
        evidence_spans=[EvidenceSpan(start=0, end=len(text), text=text)],
    )
    return ContextBundle(
        project_id=project_id,
        query="What is the UPS autonomy?",
        chunks=[chunk],
        total_tokens=12,
        max_context_tokens=4_000,
    )


def generated(value: str = "15", citation_id: str = "C1") -> dict:
    claim = f"UPS-A battery autonomy is {value} minutes."
    return {
        "answer": f"Fact: {claim} [{citation_id}]",
        "citation_ids": [citation_id],
        "claims": [{"text": claim, "type": "fact", "citation_ids": [citation_id]}],
        "confidence": 0.95,
        "status": "ANSWERED",
        "missing_information": [],
    }


def mixed_currency_context() -> ContextBundle:
    bundle = context("Approved schedule: 35-day delay.")
    bundle.query = "What is the active schedule risk?"
    bundle.chunks[0].attributes = {"approval_status": "approved"}
    proposed_text = "Switch vendor immediately."
    proposed_document_id = uuid.uuid4()
    bundle.chunks.append(
        ContextChunk(
            chunk_id="chunk-proposed",
            parent_id=uuid.uuid4(),
            document_id=proposed_document_id,
            document_type="change_order",
            project_id=bundle.project_id,
            page=1,
            section="Mitigation",
            text=proposed_text,
            score=0.8,
            dense_rank=2,
            bm25_rank=2,
            rrf_score=0.02,
            citation=Citation(
                document_id=proposed_document_id,
                filename="CO-Proposed.md",
                page=1,
                section="Mitigation",
            ),
            attributes={"approval_status": "proposed"},
            rerank_score=0.8,
            evidence_spans=[EvidenceSpan(start=0, end=len(proposed_text), text=proposed_text)],
        )
    )
    return bundle


def requested_non_current_context(status: str, query: str) -> ContextBundle:
    bundle = context(f"Switch vendor immediately under the {status} recovery measure.")
    bundle.query = query
    bundle.chunks[0].attributes = {"approval_status": status}
    return bundle


@pytest.mark.asyncio
async def test_generates_mapped_citations_from_selected_context_only() -> None:
    gateway = FakeGateway(generated())
    result = await GeminiResponder(Settings(), gateway).answer("What is the UPS autonomy?", context())

    assert result.status == "ANSWERED"
    assert result.citations[0].citation_id == "C1"
    assert result.citations[0].chunk_id == "chunk-15"
    assert result.claims[0].type == "fact"
    assert result.claims[0].support_status == "SUPPORTED"
    assert result.citations[0].supporting_spans[0].text == "UPS-A battery autonomy shall be 15 minutes."
    assert gateway.calls[0][2] is True
    assert "15 minutes" in gateway.calls[0][1]
    assert "conversation history" not in gateway.calls[0][1].lower()


@pytest.mark.asyncio
async def test_rejects_invented_factual_value() -> None:
    gateway = FakeGateway(generated("20"))
    result = await GeminiResponder(Settings(), gateway).answer("What is the UPS autonomy?", context())

    assert result.status == "INSUFFICIENT_EVIDENCE"
    assert result.claims == []
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_rejects_invented_unit_for_real_numeric_value() -> None:
    response = generated()
    response["answer"] = "UPS-A battery autonomy is 15 hours. [C1]"
    response["claims"][0]["text"] = "UPS-A battery autonomy is 15 hours."

    result = await GeminiResponder(Settings(), FakeGateway(response)).answer("What is the UPS autonomy?", context())

    assert result.status == "INSUFFICIENT_EVIDENCE"


@pytest.mark.asyncio
async def test_rejects_invalid_citation_id() -> None:
    with pytest.raises(IngestionError, match="unknown citation") as error:
        await GeminiResponder(Settings(), FakeGateway(generated(citation_id="C9"))).answer(
            "What is the UPS autonomy?", context()
        )

    assert error.value.code == "invalid_citation"


@pytest.mark.asyncio
async def test_returns_insufficient_without_calling_model_for_empty_context() -> None:
    bundle = context()
    bundle.chunks = []
    gateway = FakeGateway(generated())

    result = await GeminiResponder(Settings(), gateway).answer("Unknown value?", bundle)

    assert result.status == "INSUFFICIENT_EVIDENCE"
    assert result.citations == []
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_reports_context_revision_conflict() -> None:
    bundle = context()
    bundle.revision_conflicts = [
        RevisionConflict(
            document_key="ups_spec",
            section="2.2 battery autonomy",
            document_ids=[uuid.uuid4(), uuid.uuid4()],
            revisions=["a", "b"],
        )
    ]

    result = await GeminiResponder(Settings(), FakeGateway(generated())).answer(bundle.query, bundle)

    assert result.status == "CONFLICTING_EVIDENCE"
    assert result.answer.startswith("Conflicting revisions")
    assert result.conflicting_sources == bundle.revision_conflicts


@pytest.mark.asyncio
async def test_uses_one_semantic_call_only_for_uncertain_claim() -> None:
    response = generated()
    response["answer"] = "Battery performance is adequate. [C1]"
    response["claims"] = [
        {"text": "Battery performance is adequate.", "type": "fact", "citation_ids": ["C1"]}
    ]
    gateway = FakeGateway(
        [response, {"decisions": [{"claim_index": 0, "status": "SUPPORTED"}]}]
    )

    result = await GeminiResponder(Settings(), gateway).answer("Is battery performance adequate?", context())

    assert result.status == "ANSWERED"
    assert result.claims[0].support_status == "SUPPORTED"
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_generation_outage_returns_valid_retrieved_evidence() -> None:
    class UnavailableResponder:
        async def generate(self, *_args, **_kwargs) -> object:
            raise IngestionError("generation_unavailable", "AI provider unavailable", 503)

    service = KnowledgeService(Settings(), None, None, responder=UnavailableResponder())
    result = await service._generate_answer("How long is the backup runtime?", context())

    assert result.status == "PARTIAL"
    assert result.citations[0].citation_id == "C1"
    assert result.citations[0].supporting_spans[0].text == "UPS-A battery autonomy shall be 15 minutes."
    assert "[C1]" in result.answer


@pytest.mark.asyncio
async def test_generated_answer_never_receives_unrequested_proposed_supplemental_evidence() -> None:
    bundle = mixed_currency_context()
    response = {
        "answer": "The active schedule risk is a 35-day delay. [C1]",
        "citation_ids": ["C1"],
        "claims": [
            {"text": "The active schedule risk is a 35-day delay.", "type": "fact", "citation_ids": ["C1"]}
        ],
        "confidence": 0.9,
        "status": "ANSWERED",
        "missing_information": [],
    }
    gateway = FakeGateway(response)

    result = await GeminiResponder(Settings(), gateway).answer(bundle.query, bundle)

    evidence = json.loads(gateway.calls[0][1])["evidence"]
    assert result.status == "ANSWERED"
    assert [item["document"] for item in evidence] == ["UPS_Specification.md"]
    assert "Switch vendor immediately" not in gateway.calls[0][1]


@pytest.mark.asyncio
async def test_provider_outage_fallback_omits_unrequested_proposed_supplemental_evidence() -> None:
    class UnavailableResponder:
        async def generate(self, *_args, **_kwargs) -> object:
            raise IngestionError("generation_unavailable", "AI provider unavailable", 503)

    bundle = mixed_currency_context()
    service = KnowledgeService(Settings(), None, None, responder=UnavailableResponder())

    result = await service._generate_answer(bundle.query, bundle)

    assert result.status == "PARTIAL"
    assert [citation.chunk_id for citation in result.citations] == ["chunk-15"]
    assert [claim.text for claim in result.claims] == ["Approved schedule: 35-day delay."]
    assert "Switch vendor immediately" not in result.answer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "query"),
    [
        ("proposed", "What recovery measure is proposed?"),
        ("issued for review", "What was issued for review?"),
    ],
)
async def test_gemini_receives_explicit_non_current_evidence_with_status_label(status: str, query: str) -> None:
    response = {
        "answer": f"The {status} measure is to switch vendor. [C1]",
        "citation_ids": ["C1"],
        "claims": [
            {"text": f"The {status} measure is to switch vendor.", "type": "fact", "citation_ids": ["C1"]}
        ],
        "confidence": 0.9,
        "status": "ANSWERED",
        "missing_information": [],
    }
    gateway = FakeGateway(response)

    result = await GeminiResponder(Settings(), gateway).answer(
        query,
        requested_non_current_context(status, query),
    )

    assert result.status == "ANSWERED"
    assert f"{status.title()} document fact:" in result.answer
    evidence = json.loads(gateway.calls[0][1])["evidence"]
    assert evidence[0]["approval_status"] == status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "query"),
    [
        ("proposed", "What recovery measure is proposed?"),
        ("issued for review", "What was issued for review?"),
    ],
)
async def test_provider_outage_labels_explicit_non_current_evidence(status: str, query: str) -> None:
    class UnavailableResponder:
        async def generate(self, *_args, **_kwargs) -> object:
            raise IngestionError("generation_unavailable", "AI provider unavailable", 503)

    service = KnowledgeService(Settings(), None, None, responder=UnavailableResponder())

    result = await service._generate_answer(query, requested_non_current_context(status, query))

    assert result.status == "PARTIAL"
    assert f"{status.title()} document fact:" in result.answer
    assert "Switch vendor immediately" in result.answer
