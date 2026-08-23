"""Add deterministic attempt ordering and fenced ingestion ownership."""

from alembic import op
import sqlalchemy as sa

revision = "20260721_10"
down_revision = "20260721_09"
branch_labels = None
depends_on = None

ATTEMPT_CONSTRAINT = "uq_ingestion_jobs_document_attempt"
ATTEMPT_COLUMNS = {"document_id", "attempt_number"}
DOCUMENT_OWNER_COLUMNS = (
    ("active_ingestion_job_id", sa.Uuid()),
    ("ingestion_owner_token", sa.Uuid()),
)
JOB_OWNER_COLUMNS = (
    ("owner_token", sa.Uuid()),
    ("lease_expires_at", sa.DateTime(timezone=True)),
)


def _column_names(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _index_names(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _attempt_constraint() -> dict | None:
    for constraint in sa.inspect(op.get_bind()).get_unique_constraints("ingestion_jobs"):
        if set(constraint.get("column_names") or []) == ATTEMPT_COLUMNS:
            return constraint
    return None


def _add_owner_columns() -> None:
    document_columns = _column_names("documents")
    for name, column_type in DOCUMENT_OWNER_COLUMNS:
        if name not in document_columns:
            op.add_column("documents", sa.Column(name, column_type, nullable=True))
    job_columns = _column_names("ingestion_jobs")
    for name, column_type in JOB_OWNER_COLUMNS:
        if name not in job_columns:
            op.add_column("ingestion_jobs", sa.Column(name, column_type, nullable=True))
    for table, name in (
        ("documents", "active_ingestion_job_id"),
        ("documents", "ingestion_owner_token"),
        ("ingestion_jobs", "owner_token"),
    ):
        index_name = f"ix_{table}_{name}"
        if index_name not in _index_names(table):
            op.create_index(index_name, table, [name])


def _attempt_rows() -> list[tuple[object, object, int | None]]:
    jobs = sa.table(
        "ingestion_jobs",
        sa.column("id", sa.Uuid()),
        sa.column("document_id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("attempt_number", sa.Integer()),
    )
    return list(
        op.get_bind().execute(
            sa.select(jobs.c.id, jobs.c.document_id, jobs.c.attempt_number).order_by(
                jobs.c.document_id, jobs.c.created_at, jobs.c.id
            )
        )
    )


def _attempts_need_backfill(rows: list[tuple[object, object, int | None]]) -> bool:
    seen: set[tuple[object, int]] = set()
    for _, document_id, attempt in rows:
        if attempt is None or attempt < 1 or (document_id, attempt) in seen:
            return True
        seen.add((document_id, attempt))
    return False


def _backfill_attempts(rows: list[tuple[object, object, int | None]]) -> None:
    jobs = sa.table(
        "ingestion_jobs",
        sa.column("id", sa.Uuid()),
        sa.column("attempt_number", sa.Integer()),
    )
    counters: dict[object, int] = {}
    for job_id, document_id, _ in rows:
        counters[document_id] = counters.get(document_id, 0) + 1
        op.get_bind().execute(
            jobs.update().where(jobs.c.id == job_id).values(attempt_number=counters[document_id])
        )


def upgrade() -> None:
    if "attempt_number" not in _column_names("ingestion_jobs"):
        op.add_column("ingestion_jobs", sa.Column("attempt_number", sa.Integer(), nullable=True))
    _add_owner_columns()

    rows = _attempt_rows()
    constraint = _attempt_constraint()
    if _attempts_need_backfill(rows):
        if constraint and constraint.get("name"):
            with op.batch_alter_table("ingestion_jobs") as batch:
                batch.drop_constraint(constraint["name"], type_="unique")
            constraint = None
        _backfill_attempts(rows)

    attempt_column = next(
        column for column in sa.inspect(op.get_bind()).get_columns("ingestion_jobs")
        if column["name"] == "attempt_number"
    )
    default = str(attempt_column.get("default") or "").strip("'() ")
    needs_alter = attempt_column.get("nullable", True) or default != "1"
    if needs_alter or not constraint:
        with op.batch_alter_table("ingestion_jobs") as batch:
            if needs_alter:
                batch.alter_column(
                    "attempt_number",
                    existing_type=sa.Integer(),
                    nullable=False,
                    server_default=sa.text("1"),
                )
            if not constraint:
                batch.create_unique_constraint(
                    ATTEMPT_CONSTRAINT,
                    ["document_id", "attempt_number"],
                )


def downgrade() -> None:
    for table, name in (
        ("documents", "active_ingestion_job_id"),
        ("documents", "ingestion_owner_token"),
        ("ingestion_jobs", "owner_token"),
    ):
        index_name = f"ix_{table}_{name}"
        if index_name in _index_names(table):
            op.drop_index(index_name, table_name=table)

    job_columns = _column_names("ingestion_jobs")
    constraint = _attempt_constraint()
    with op.batch_alter_table("ingestion_jobs") as batch:
        if constraint and constraint.get("name"):
            batch.drop_constraint(constraint["name"], type_="unique")
        for name, _ in JOB_OWNER_COLUMNS:
            if name in job_columns:
                batch.drop_column(name)
        if "attempt_number" in job_columns:
            batch.drop_column("attempt_number")

    document_columns = _column_names("documents")
    with op.batch_alter_table("documents") as batch:
        for name, _ in DOCUMENT_OWNER_COLUMNS:
            if name in document_columns:
                batch.drop_column(name)
