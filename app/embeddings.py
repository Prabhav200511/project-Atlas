import asyncio
from typing import Protocol

from fastembed import TextEmbedding

from app.errors import IngestionError


class Embedder(Protocol):
    dimensions: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_queries(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    def __init__(self, model_name: str, dimensions: int, cache_dir: str | None = None) -> None:
        self.model_name = model_name
        self.dimensions = dimensions
        self.cache_dir = cache_dir
        self._model: TextEmbedding | None = None
        self._load_lock = asyncio.Lock()

    async def warmup(self) -> None:
        await self._get_model()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        model = await self._get_model()
        return await asyncio.to_thread(self._encode_passages, model, texts)

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        model = await self._get_model()
        return await asyncio.to_thread(self._encode_queries, model, texts)

    async def _get_model(self) -> TextEmbedding:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                model = await asyncio.to_thread(
                    TextEmbedding,
                    model_name=self.model_name,
                    cache_dir=self.cache_dir,
                )
                actual = int(model.embedding_size)
                if actual != self.dimensions:
                    raise IngestionError(
                        "invalid_embedding_configuration",
                        "Configured embedding dimensions do not match the model",
                        500,
                        {"model": self.model_name, "expected_dimensions": self.dimensions, "actual_dimensions": actual},
                    )
                self._model = model
        assert self._model is not None
        return self._model

    def _encode_passages(self, model: TextEmbedding, texts: list[str]) -> list[list[float]]:
        return self._vectors(model.passage_embed(texts))

    def _encode_queries(self, model: TextEmbedding, texts: list[str]) -> list[list[float]]:
        return self._vectors(model.query_embed(texts))

    def _vectors(self, values) -> list[list[float]]:
        vectors = [[float(item) for item in value] for value in values]
        if any(len(vector) != self.dimensions for vector in vectors):
            raise IngestionError("invalid_embedding", "Embedding response did not match configured dimensions", 502)
        return vectors
