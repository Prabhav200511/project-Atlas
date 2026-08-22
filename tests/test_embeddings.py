import pytest

import app.embeddings as embeddings
from app.embeddings import FastEmbedder
from app.ingestion import IngestionError


class FakeModel:
    embedding_size = 3

    def __init__(self, *, model_name: str, cache_dir: str | None = None) -> None:
        self.model_name, self.cache_dir = model_name, cache_dir

    def passage_embed(self, texts: list[str]):
        return iter([[1.0, 0.0, float(index)] for index, _ in enumerate(texts)])

    def query_embed(self, texts: list[str]):
        return iter([[0.0, 1.0, float(index)] for index, _ in enumerate(texts)])


@pytest.mark.asyncio
async def test_fastembed_uses_distinct_passage_and_query_encoders(monkeypatch) -> None:
    monkeypatch.setattr(embeddings, "TextEmbedding", FakeModel)
    embedder = FastEmbedder("test/model", 3, "./cache")

    assert await embedder.embed_documents(["document"]) == [[1.0, 0.0, 0.0]]
    assert await embedder.embed_queries(["question"]) == [[0.0, 1.0, 0.0]]
    assert embedder.dimensions == 3


@pytest.mark.asyncio
async def test_fastembed_rejects_model_dimension_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(embeddings, "TextEmbedding", FakeModel)
    embedder = FastEmbedder("test/model", 384)

    with pytest.raises(IngestionError) as error:
        await embedder.warmup()

    assert error.value.code == "invalid_embedding_configuration"
    assert error.value.details == {"model": "test/model", "expected_dimensions": 384, "actual_dimensions": 3}
