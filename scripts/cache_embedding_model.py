"""Download and validate the configured semantic embedding model for deployment."""

import asyncio

from app.config import get_settings
from app.embeddings import FastEmbedder


async def warm() -> None:
    settings = get_settings()
    embedder = FastEmbedder(
        settings.embedding_model,
        settings.embedding_dimensions,
        settings.embedding_cache_dir,
    )
    await embedder.warmup()
    print(f"cached {settings.embedding_model} ({settings.embedding_dimensions} dimensions)")


if __name__ == "__main__":
    asyncio.run(warm())
