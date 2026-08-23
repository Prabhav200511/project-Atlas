"""Download and validate the configured semantic embedding model for deployment."""

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
