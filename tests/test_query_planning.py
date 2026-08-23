import json
import uuid

import pytest

from app.config import Settings
from app.ingestion import IngestionError
from app.workflow import ConversationMessage, GeminiQueryPlanner, KnowledgeService


@pytest.mark.asyncio
async def test_standalone_question_preserves_original_query() -> None:
    project_id = uuid.uuid4()
    plan = await GeminiQueryPlanner(Settings()).plan(project_id, "What is the UPS-A battery autonomy?", [])

    assert plan.original_query == "What is the UPS-A battery autonomy?"
    assert plan.standalone_query == plan.original_query
    assert plan.project_id == project_id
    assert plan.equipment_ids == ["UPS-A"]


@pytest.mark.asyncio
async def test_follow_up_uses_recent_conversation_context() -> None:
    plan = await GeminiQueryPlanner(Settings()).plan(
        uuid.uuid4(),
        "What about its voltage?",
        [ConversationMessage(role="user", content="What are the UPS-A battery requirements?")],
    )

    assert "UPS-A" in plan.standalone_query
    assert plan.original_query == "What about its voltage?"


@pytest.mark.asyncio
async def test_subqueries_are_limited_to_genuinely_multi_part_questions() -> None:
    planner, project_id = GeminiQueryPlanner(Settings()), uuid.uuid4()

    single = await planner.plan(project_id, "What is UPS-A battery autonomy?", [])
    multi = await planner.plan(
        project_id,
        "What is UPS-A autonomy and what is its voltage; what is its clearance; what is its frequency?",
        [],
    )

    assert single.subqueries == []
    assert len(multi.subqueries) == 3


@pytest.mark.asyncio
async def test_ambiguous_filters_are_left_empty() -> None:
    plan = await GeminiQueryPlanner(Settings()).plan(uuid.uuid4(), "Show vendor documents.", [])

    assert plan.document_types == []
    assert plan.document_ids == []
    assert plan.vendor_ids == []
    assert plan.revision_status is None


@pytest.mark.asyncio
async def test_planner_falls_back_when_model_gateway_is_unavailable() -> None:
    class UnavailableGateway:
        is_available = True

        async def generate(self, *_args, **_kwargs) -> str:
            raise IngestionError("model_gateway_error", "AI provider request failed", 502)

    plan = await GeminiQueryPlanner(Settings(), UnavailableGateway()).plan(
        uuid.uuid4(), "What is UPS-A battery autonomy?", []
    )

    assert plan.standalone_query == "What is UPS-A battery autonomy?"
    assert plan.equipment_ids == ["UPS-A"]


@pytest.mark.asyncio
async def test_local_planner_does_not_turn_approval_yes_no_question_into_retrieval_filter() -> None:
    plan = await GeminiQueryPlanner(Settings()).plan(
        uuid.uuid4(),
        "Is a switchgear delivery recovery measure approved?",
        [],
    )

    assert plan.revision_status is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Can you show proposed recovery measures?", "proposed"),
        ("Show records issued for review.", "issued for review"),
        ("Tell me whether the approved submittal meets the rating.", None),
        ("What measures are not approved?", None),
        ("Do not use proposed documents.", None),
        ("The RFI body says Status: Proposed. What cable size is required?", None),
        ("Proposed answer: use the approved specification. What is the required rating?", None),
        ("What clearance is required? Proposed answer: use an approved cable.", None),
    ],
)
async def test_local_revision_status_requires_explicit_positive_selection(query: str, expected: str | None) -> None:
    plan = await GeminiQueryPlanner(Settings()).plan(uuid.uuid4(), query, [])

    assert plan.revision_status == expected


class FakeGateway:
    client = object()

    async def generate(self, *_args, **_kwargs) -> str:
        return json.dumps(
            {
                "original_query": "other query",
                "standalone_query": "UPS-A document",
                "intent": "knowledge_query",
                "project_id": str(uuid.uuid4()),
                "document_types": ["specification"],
                "document_ids": [str(uuid.uuid4())],
                "equipment_ids": ["UPS-A", "SWGR-A"],
                "vendor_ids": ["UnmentionedVendor"],
                "revision_status": "approved",
                "section": "2.2",
                "subqueries": ["UPS-A document"],
            }
        )


@pytest.mark.asyncio
async def test_planner_enforces_project_isolation_and_supported_ids() -> None:
    project_id = uuid.uuid4()
    plan = await GeminiQueryPlanner(Settings(), FakeGateway()).plan(project_id, "Find UPS-A information.", [])

    assert plan.project_id == project_id
    assert plan.original_query == "Find UPS-A information."
    assert plan.document_ids == []
    assert plan.equipment_ids == ["UPS-A"]
    assert plan.vendor_ids == []
    assert plan.revision_status is None
    assert plan.section is None


@pytest.mark.asyncio
async def test_schedule_query_routes_to_existing_schedule_service() -> None:
    project_id = uuid.uuid4()
    route = await KnowledgeService(Settings(), None, None).route_query(project_id, "Show critical path delay risk.", [])

    assert route.plan.intent == "schedule_query"
    assert route.service == "schedule"
    assert route.endpoint == f"/projects/{project_id}/schedule/analysis"


@pytest.mark.asyncio
async def test_route_plan_is_explicitly_advisory_only() -> None:
    project_id = uuid.uuid4()
    route = await KnowledgeService(Settings(), None, None).route_query(project_id, "Show critical path risk", [])

    assert route.execution_mode == "advisory_only"
    assert route.endpoint == f"/projects/{project_id}/schedule/analysis"
