import asyncio
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.errors import IngestionError
from app.ingestion import _claim_ingestion, _mark_failed, recover_ingestion_attempt
from app.models import Base, Document, IngestionJob, Project


async def seed_processing_attempt(
    sessions: async_sessionmaker,
    tmp_path: Path,
    *,
    owned: bool,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    project_id, document_id, job_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    owner_token = uuid.uuid4() if owned else None
    async with sessions() as session:
        session.add(Project(id=project_id, name="Operator recovery"))
        session.add(
            Document(
                id=document_id,
                project_id=project_id,
                filename="recovery.md",
                storage_path=str(tmp_path / "recovery.md"),
                document_type="RFI",
                status="processing",
                content_sha256="a" * 64,
                mime_type="text/markdown",
                size_bytes=1,
                metadata_json={"preserved": True},
                active_ingestion_job_id=job_id if owned else None,
                ingestion_owner_token=owner_token,
            )
        )
        session.add(
            IngestionJob(
                id=job_id,
                project_id=project_id,
                document_id=document_id,
                attempt_number=1,
                attempt_count=1,
                status="processing",
                owner_token=owner_token,
                lease_expires_at=datetime.now(UTC) - timedelta(days=1) if owned else None,
            )
        )
        await session.commit()
    return project_id, document_id, job_id


async def state(sessions: async_sessionmaker, document_id: uuid.UUID, job_id: uuid.UUID) -> tuple:
    async with sessions() as session:
        document = await session.get(Document, document_id)
        job = await session.get(IngestionJob, job_id)
        assert document is not None and job is not None
        return (
            document.status,
            document.active_ingestion_job_id,
            document.ingestion_owner_token,
            document.metadata_json,
            job.status,
            job.owner_token,
            job.lease_expires_at,
            job.attempt_count,
            job.error,
        )


async def seed_ambiguous_processing_attempts(
    sessions: async_sessionmaker,
    tmp_path: Path,
    *,
    active_index: int | None,
) -> tuple[uuid.UUID, tuple[uuid.UUID, uuid.UUID]]:
    project_id, document_id = uuid.uuid4(), uuid.uuid4()
    job_ids = (uuid.uuid4(), uuid.uuid4())
    owner_tokens = (uuid.uuid4(), uuid.uuid4())
    async with sessions() as session:
        session.add(Project(id=project_id, name="Ambiguous operator recovery"))
        session.add(
            Document(
                id=document_id,
                project_id=project_id,
                filename="ambiguous.md",
                storage_path=str(tmp_path / "ambiguous.md"),
                document_type="RFI",
                status="processing",
                content_sha256="b" * 64,
                mime_type="text/markdown",
                size_bytes=1,
                metadata_json={"preserved": True},
                active_ingestion_job_id=job_ids[active_index] if active_index is not None else None,
                ingestion_owner_token=owner_tokens[active_index] if active_index is not None else None,
            )
        )
        session.add_all(
            [
                IngestionJob(
                    id=job_id,
                    project_id=project_id,
                    document_id=document_id,
                    attempt_number=index + 1,
                    attempt_count=1,
                    status="processing",
                    owner_token=owner_tokens[index],
                )
                for index, job_id in enumerate(job_ids)
            ]
        )
        await session.commit()
    return document_id, job_ids


async def processing_state(sessions: async_sessionmaker, document_id: uuid.UUID) -> tuple:
    async with sessions() as session:
        document = await session.get(Document, document_id)
        jobs = list(
            (
                await session.scalars(
                    select(IngestionJob)
                    .where(IngestionJob.document_id == document_id)
                    .order_by(IngestionJob.attempt_number)
                )
            ).all()
        )
        assert document is not None
        return (
            document.status,
            document.active_ingestion_job_id,
            document.ingestion_owner_token,
            document.metadata_json,
            [
                (
                    job.id,
                    job.attempt_number,
                    job.status,
                    job.owner_token,
                    job.attempt_count,
                    job.error,
                )
                for job in jobs
            ],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("owned", [True, False], ids=["owned", "revision09-unowned"])
async def test_operator_recovery_requires_confirmation_then_preserves_history_as_failed(
    tmp_path: Path,
    owned: bool,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'recovery-{owned}.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        _, document_id, job_id = await seed_processing_attempt(sessions, tmp_path, owned=owned)
        before = await state(sessions, document_id, job_id)

        async with sessions() as session:
            with pytest.raises(IngestionError) as error:
                await recover_ingestion_attempt(
                    session,
                    document_id=document_id,
                    job_id=job_id,
                    confirm_worker_stopped=False,
                )
        assert error.value.code == "operator_confirmation_required"
        assert await state(sessions, document_id, job_id) == before

        async with sessions() as session:
            recovered = await recover_ingestion_attempt(
                session,
                document_id=document_id,
                job_id=job_id,
                confirm_worker_stopped=True,
            )

        assert recovered == {"document_id": str(document_id), "job_id": str(job_id), "status": "failed"}
        after = await state(sessions, document_id, job_id)
        assert after[:4] == ("failed", None, None, {"preserved": True})
        assert after[4:8] == ("failed", None, None, 1)
        assert after[8] == "Operator confirmed the worker stopped; attempt is restartable"

        async with sessions() as session:
            document = await session.get(Document, document_id)
            job = await session.get(IngestionJob, job_id)
            assert document is not None and job is not None
            retry_owner = await _claim_ingestion(session, document, job)
            assert job.attempt_count == 2
            assert await _mark_failed(session, document_id, job_id, retry_owner, "synthetic retry stop") is True
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active_index", "supplied_index", "expected_code"),
    [
        (None, 0, "ingestion_recovery_ambiguous"),
        (None, 1, "ingestion_recovery_ambiguous"),
        (0, 0, "ingestion_recovery_ambiguous"),
        (1, 1, "ingestion_recovery_ambiguous"),
        (0, 1, "ingestion_recovery_conflict"),
        (1, 0, "ingestion_recovery_conflict"),
    ],
)
async def test_operator_recovery_rejects_ambiguous_or_wrong_processing_job_without_mutation(
    tmp_path: Path,
    active_index: int | None,
    supplied_index: int,
    expected_code: str,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / f'ambiguous-{active_index}-{supplied_index}.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        document_id, job_ids = await seed_ambiguous_processing_attempts(
            sessions,
            tmp_path,
            active_index=active_index,
        )
        before = await processing_state(sessions, document_id)

        async with sessions() as session:
            with pytest.raises(IngestionError) as error:
                await recover_ingestion_attempt(
                    session,
                    document_id=document_id,
                    job_id=job_ids[supplied_index],
                    confirm_worker_stopped=True,
                )

        assert error.value.code == expected_code
        assert await processing_state(sessions, document_id) == before
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_operator_recovery_has_one_winner_and_one_structured_conflict(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'concurrent-recovery.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        _, document_id, job_id = await seed_processing_attempt(sessions, tmp_path, owned=False)

        async def recover() -> dict[str, str] | IngestionError:
            async with sessions() as session:
                try:
                    return await recover_ingestion_attempt(
                        session,
                        document_id=document_id,
                        job_id=job_id,
                        confirm_worker_stopped=True,
                    )
                except IngestionError as error:
                    return error

        results = await asyncio.gather(recover(), recover())

        assert sum(isinstance(result, dict) for result in results) == 1
        errors = [result for result in results if isinstance(result, IngestionError)]
        assert len(errors) == 1
        assert errors[0].code == "ingestion_recovery_conflict"
        final = await state(sessions, document_id, job_id)
        assert final[:4] == ("failed", None, None, {"preserved": True})
        assert final[4:8] == ("failed", None, None, 1)
    finally:
        await engine.dispose()


def test_recovery_cli_refuses_without_confirmation_then_recovers_when_confirmed(tmp_path: Path) -> None:
    database = tmp_path / "cli.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def setup() -> tuple[uuid.UUID, uuid.UUID]:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        _, document_id, job_id = await seed_processing_attempt(sessions, tmp_path, owned=True)
        return document_id, job_id

    document_id, job_id = asyncio.run(setup())
    before = asyncio.run(state(sessions, document_id, job_id))
    environment = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}"}

    result = subprocess.run(
        [
            sys.executable,
            "scripts/recover_ingestion.py",
            "--document-id",
            str(document_id),
            "--job-id",
            str(job_id),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--confirm-worker-stopped is required" in result.stderr
    assert asyncio.run(state(sessions, document_id, job_id)) == before

    confirmed = subprocess.run(
        [
            sys.executable,
            "scripts/recover_ingestion.py",
            "--document-id",
            str(document_id),
            "--job-id",
            str(job_id),
            "--confirm-worker-stopped",
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert confirmed.returncode == 0
    assert f'"job_id": "{job_id}"' in confirmed.stdout
    assert asyncio.run(state(sessions, document_id, job_id))[0:2] == ("failed", None)
    asyncio.run(engine.dispose())
