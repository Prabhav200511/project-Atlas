from types import SimpleNamespace

import pytest
from google.genai import errors

from app.compliance import ComplianceExplainer
from app.config import Settings
from app.ingestion import IngestionError
from app.llm import GeminiGateway
from app.schedule import ScheduleNarrator


class FailingModels:
    async def generate_content(self, **_kwargs):
        raise errors.ClientError(400, {"error": {"message": "invalid key"}})


@pytest.mark.asyncio
async def test_default_groq_request_uses_production_gpt_oss_model(monkeypatch) -> None:
    captured_payload: dict = {}

    class SuccessfulResponse:
        status_code = 200

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "grounded response"}}]}

    class RecordingAsyncClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            pass

        async def post(self, _url, *, json, headers):
            captured_payload.update(json)
            return SuccessfulResponse()

    monkeypatch.setattr("app.llm.httpx.AsyncClient", RecordingAsyncClient)
    gateway = GeminiGateway(Settings(groq_api_key="test-key"))

    response = await gateway.generate("instructions", "content")

    assert response == "grounded response"
    assert captured_payload["model"] == "openai/gpt-oss-120b"


@pytest.mark.asyncio
async def test_invalid_api_key_becomes_safe_gateway_error() -> None:
    gateway = GeminiGateway(Settings(gemini_api_key="invalid"))
    gateway.client = SimpleNamespace(models=FailingModels())

    with pytest.raises(IngestionError) as caught:
        await gateway.generate("instructions", "content")

    assert caught.value.code == "model_gateway_error"
    assert caught.value.status_code == 502
    assert caught.value.message.startswith("AI provider request failed")


@pytest.mark.asyncio
async def test_optional_compliance_explanation_falls_back_to_deterministic_text() -> None:
    explainer = ComplianceExplainer(Settings(gemini_api_key="invalid"))
    explainer.gateway.client = SimpleNamespace(models=FailingModels())
    draft = SimpleNamespace(explanation="Deterministic result.", model_dump=lambda **_kwargs: {})

    assert await explainer.explain(draft) == "Deterministic result."


@pytest.mark.asyncio
async def test_optional_schedule_narrative_falls_back_to_deterministic_result() -> None:
    narrator = ScheduleNarrator(Settings(gemini_api_key="invalid"))
    narrator.gateway.client = SimpleNamespace(models=FailingModels())
    risk = SimpleNamespace(model_dump=lambda **_kwargs: {})

    assert await narrator.enrich(risk) is risk
