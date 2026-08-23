import asyncio
import sqlite3
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.ingestion import recover_ingestion_attempt


def migrate(monkeypatch: pytest.MonkeyPatch, database: Path, revision: str) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    get_settings.cache_clear()
    command.upgrade(Config(str(Path(__file__).parents[1] / "alembic.ini")), revision) if revision != "20260721_09" else command.downgrade(
        Config(str(Path(__file__).parents[1] / "alembic.ini")), revision
    )
    get_settings.cache_clear()


def seed_legacy_jobs(database: Path) -> tuple[str, list[str]]:
    project_id = uuid.uuid4().hex
    document_id = uuid.uuid4().hex
    job_ids = [uuid.uuid4().hex for _ in range(3)]
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO projects (id, name) VALUES (?, ?)", (project_id, "Migration project"))
        connection.execute(
            """INSERT INTO documents
               (id, project_id, filename, storage_path, document_type, status, content_sha256,
                mime_type, size_bytes, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (document_id, project_id, "legacy.md", "legacy.md", "RFI", "failed", "a" * 64, "text/markdown", 1, "{}"),
        )
        for index, job_id in enumerate(job_ids):
            connection.execute(
                """INSERT INTO ingestion_jobs
                   (id, project_id, document_id, status, chunk_count, attempt_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (job_id, project_id, document_id, "failed", 0, 1, f"2026-08-22 12:00:0{index}"),
            )
    return document_id, job_ids


def seed_legacy_processing(database: Path) -> tuple[str, str]:
    project_id, document_id, job_id = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO projects (id, name) VALUES (?, ?)", (project_id, "Legacy processing"))
        connection.execute(
            """INSERT INTO documents
               (id, project_id, filename, storage_path, document_type, status, content_sha256,
                mime_type, size_bytes, metadata)
               VALUES (?, ?, 'processing.md', 'processing.md', 'RFI', 'processing', ?,
                       'text/markdown', 1, '{}')""",
            (document_id, project_id, "b" * 64),
        )
        connection.execute(
            """INSERT INTO ingestion_jobs
               (id, project_id, document_id, status, chunk_count, attempt_count, created_at)
               VALUES (?, ?, ?, 'processing', 0, 1, '2026-08-22 12:00:00')""",
            (job_id, project_id, document_id),
        )
    return document_id, job_id


def attempt_invariant(database: Path, document_id: str) -> tuple[list[int], bool, str | None, bool]:
    with sqlite3.connect(database) as connection:
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(ingestion_jobs)")}
        attempts = [
            row[0]
            for row in connection.execute(
                "SELECT attempt_number FROM ingestion_jobs WHERE document_id = ? ORDER BY created_at, id",
                (document_id,),
            )
        ]
        unique = any(
            {item[2] for item in connection.execute(f"PRAGMA index_info('{row[1]}')")} == {
                "document_id",
                "attempt_number",
            }
            and row[2] == 1
            for row in connection.execute("PRAGMA index_list('ingestion_jobs')")
        )
        return attempts, columns["attempt_number"][3] == 1, columns["attempt_number"][4], unique


def test_revision_09_sqlite_upgrade_enforces_attempt_and_owner_invariants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "upgrade.db"
    migrate(monkeypatch, database, "head")
    migrate(monkeypatch, database, "20260721_09")
    document_id, _ = seed_legacy_jobs(database)

    migrate(monkeypatch, database, "head")

    assert attempt_invariant(database, document_id) == ([1, 2, 3], True, "1", True)
    with sqlite3.connect(database) as connection:
        document_columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
        job_columns = {row[1] for row in connection.execute("PRAGMA table_info(ingestion_jobs)")}
        assert {"active_ingestion_job_id", "ingestion_owner_token"} <= document_columns
        assert {"owner_token", "lease_expires_at"} <= job_columns
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO ingestion_jobs
                   (id, project_id, document_id, attempt_number, status, chunk_count, attempt_count)
                   SELECT ?, project_id, document_id, 1, 'queued', 0, 0
                   FROM ingestion_jobs WHERE document_id = ? LIMIT 1""",
                (uuid.uuid4().hex, document_id),
            )


def test_partial_revision_10_sqlite_upgrade_repairs_null_invalid_and_duplicate_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "partial.db"
    migrate(monkeypatch, database, "head")
    migrate(monkeypatch, database, "20260721_09")
    document_id, _ = seed_legacy_jobs(database)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE ingestion_jobs ADD COLUMN attempt_number INTEGER")
        connection.execute(
            "UPDATE ingestion_jobs SET attempt_number = CASE WHEN created_at LIKE '%00' THEN NULL ELSE 0 END"
        )

    migrate(monkeypatch, database, "head")

    assert attempt_invariant(database, document_id) == ([1, 2, 3], True, "1", True)


def test_sqlite_attempt_migration_downgrades_and_reupgrades_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "roundtrip.db"
    migrate(monkeypatch, database, "head")
    migrate(monkeypatch, database, "20260721_09")
    with sqlite3.connect(database) as connection:
        assert "attempt_number" not in {row[1] for row in connection.execute("PRAGMA table_info(ingestion_jobs)")}

    document_id, _ = seed_legacy_jobs(database)
    migrate(monkeypatch, database, "head")

    assert attempt_invariant(database, document_id) == ([1, 2, 3], True, "1", True)


def test_revision_09_processing_rows_remain_processing_until_confirmed_operator_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "legacy-processing.db"
    migrate(monkeypatch, database, "head")
    migrate(monkeypatch, database, "20260721_09")
    document_id, job_id = seed_legacy_processing(database)

    migrate(monkeypatch, database, "head")
    migrate(monkeypatch, database, "20260721_09")
    migrate(monkeypatch, database, "head")

    with sqlite3.connect(database) as connection:
        document = connection.execute(
            "SELECT status, active_ingestion_job_id, ingestion_owner_token FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        job = connection.execute(
            "SELECT status, owner_token, lease_expires_at, attempt_number FROM ingestion_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    assert document == ("processing", None, None)
    assert job == ("processing", None, None, 1)

    engine = create_async_engine(f"sqlite+aiosqlite:///{database.as_posix()}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def recover() -> None:
        async with sessions() as session:
            await recover_ingestion_attempt(
                session,
                document_id=uuid.UUID(document_id),
                job_id=uuid.UUID(job_id),
                confirm_worker_stopped=True,
            )
        await engine.dispose()

    asyncio.run(recover())

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT status FROM documents WHERE id = ?", (document_id,)).fetchone() == (
            "failed",
        )
        assert connection.execute(
            "SELECT status, owner_token, lease_expires_at FROM ingestion_jobs WHERE id = ?", (job_id,)
        ).fetchone() == ("failed", None, None)
