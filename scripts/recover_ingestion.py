"""Explicitly recover a processing ingestion after an operator has stopped its worker."""

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings
from app.database import create_database_engine, create_session_factory
from app.ingestion import recover_ingestion_attempt


async def run(
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    *,
    confirm_worker_stopped: bool,
) -> dict[str, str]:
    settings = get_settings()
    engine = create_database_engine(settings)
    sessions = create_session_factory(engine)
    try:
        async with sessions() as session:
            return await recover_ingestion_attempt(
                session,
                document_id=document_id,
                job_id=job_id,
                confirm_worker_stopped=confirm_worker_stopped,
            )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Move one processing ingestion to failed only after its worker has been stopped."
    )
    parser.add_argument("--document-id", required=True, type=uuid.UUID)
    parser.add_argument("--job-id", required=True, type=uuid.UUID)
    parser.add_argument("--confirm-worker-stopped", action="store_true")
    args = parser.parse_args()
    if not args.confirm_worker_stopped:
        parser.error("--confirm-worker-stopped is required; stop the owning worker before recovery")
    print(
        json.dumps(
            asyncio.run(
                run(
                    args.document_id,
                    args.job_id,
                    confirm_worker_stopped=args.confirm_worker_stopped,
                )
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
