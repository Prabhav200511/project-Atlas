"""Explicitly migrate one project's stored documents to the configured index version."""

import argparse
import asyncio
import json
import uuid

from qdrant_client import AsyncQdrantClient

from app.config import get_settings
from app.database import create_database_engine, create_session_factory
from app.embeddings import FastEmbedder
from app.ingestion import reindex_documents


async def run(project_id: uuid.UUID, document_id: uuid.UUID | None, force: bool) -> dict[str, int]:
    settings = get_settings()
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    qdrant = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key,
        check_compatibility=False,
    )
    embedder = FastEmbedder(
        settings.embedding_model,
        settings.embedding_dimensions,
        settings.embedding_cache_dir,
    )
    try:
        async with sessions() as session:
            return await reindex_documents(
                session,
                qdrant,
                embedder,
                settings,
                project_id,
                document_id,
                force=force,
            )
    finally:
        await qdrant.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Reindex old Atlas documents for one project; never runs at startup.")
    parser.add_argument("--project-id", required=True, type=uuid.UUID)
    parser.add_argument("--document-id", type=uuid.UUID)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.project_id, args.document_id, args.force)), sort_keys=True))


if __name__ == "__main__":
    main()
