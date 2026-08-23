import csv
import copy
import io
import logging
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from math import log
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import fitz
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Distance, FilterSelector, PointStruct, VectorParams
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.embeddings import Embedder
from app.errors import IngestionError
from app.models import Document, IngestionJob
from app.vector import document_filter, parent_filter, retrieval_filter, vector_payload

if TYPE_CHECKING:
    from app.graph import GraphStore
    from app.workflow import QueryPlan

logger = logging.getLogger("atlas.ingestion")

DocumentType = Literal[
    "specification",
    "submittal",
    "RFI",
    "meeting_minutes",
    "change_order",
    "schedule",
    "commissioning_record",
]
SUPPORTED_TYPES = {"specification", "submittal", "RFI", "meeting_minutes", "change_order", "schedule", "commissioning_record"}
EQUIPMENT_PATTERN = re.compile(r"\b(?:UPS-[A-Z][A-Z0-9]*|CRAC-\d+|SWGR-[A-Z][A-Z0-9]*)\b")
SPEC_PATTERN = re.compile(r"\b\d+\.\d+(?:\.\d+)?\b")


async def next_ingestion_attempt(session: AsyncSession, document_id: uuid.UUID) -> int:
    latest = await session.scalar(
        select(func.max(IngestionJob.attempt_number)).where(IngestionJob.document_id == document_id)
    )
    return int(latest or 0) + 1


async def _claim_ingestion(
    session: AsyncSession,
    document: Document,
    job: IngestionJob,
    *,
    allow_completed: bool = False,
) -> uuid.UUID:
    owner_token = uuid.uuid4()
    now = datetime.now(UTC)
    document_statuses = ["queued", "failed", *(["completed"] if allow_completed else [])]
    document_claim = await session.execute(
        update(Document)
        .where(
            Document.id == document.id,
            Document.status.in_(document_statuses),
            Document.ingestion_owner_token.is_(None),
            or_(
                Document.active_ingestion_job_id.is_(None),
                Document.active_ingestion_job_id == job.id,
            ),
        )
        .values(
            status="processing",
            active_ingestion_job_id=job.id,
            ingestion_owner_token=owner_token,
        )
        .execution_options(synchronize_session=False)
    )
    if document_claim.rowcount != 1:
        await session.rollback()
        raise IngestionError("ingestion_not_claimed", "Another ingestion runner owns this document", 409)
    job_claim = await session.execute(
        update(IngestionJob)
        .where(
            IngestionJob.id == job.id,
            IngestionJob.status.in_(["queued", "failed"]),
        )
        .values(
            status="processing",
            owner_token=owner_token,
            lease_expires_at=None,
            attempt_count=IngestionJob.attempt_count + 1,
            started_at=now,
            completed_at=None,
            error=None,
        )
        .execution_options(synchronize_session=False)
    )
    if job_claim.rowcount != 1:
        await session.rollback()
        raise IngestionError("ingestion_not_claimed", "Another ingestion runner owns this attempt", 409)
    await session.commit()
    await session.refresh(document)
    await session.refresh(job)
    return owner_token


async def _complete_ingestion(
    session: AsyncSession,
    document: Document,
    job: IngestionJob,
    owner_token: uuid.UUID,
    metadata: dict,
    chunk_count: int,
) -> bool:
    now = datetime.now(UTC)
    job_result = await session.execute(
        update(IngestionJob)
        .where(
            IngestionJob.id == job.id,
            IngestionJob.status == "processing",
            IngestionJob.owner_token == owner_token,
        )
        .values(status="completed", chunk_count=chunk_count, completed_at=now, lease_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    document_result = await session.execute(
        update(Document)
        .where(
            Document.id == document.id,
            Document.status == "processing",
            Document.active_ingestion_job_id == job.id,
            Document.ingestion_owner_token == owner_token,
        )
        .values(
            status="completed",
            page_count=int(metadata["page_count"]),
            metadata_json=metadata,
            active_ingestion_job_id=None,
            ingestion_owner_token=None,
        )
        .execution_options(synchronize_session=False)
    )
    if job_result.rowcount != 1 or document_result.rowcount != 1:
        await session.rollback()
        return False
    await session.commit()
    await session.refresh(document)
    await session.refresh(job)
    return True


@dataclass
class ExtractedPage:
    page: int
    text: str
    section: str = "General"


@dataclass
class ExtractedDocument:
    pages: list[ExtractedPage]
    metadata: dict[str, object]


@dataclass
class Chunk:
    project_id: uuid.UUID
    document_id: uuid.UUID
    document_type: str
    filename: str
    page: int
    section: str
    chunk_index: int
    text: str
    attributes: dict[str, object] | None = None
    parent_text: str | None = None

    def contextual_text(self, original_text: str | None = None) -> str:
        attributes = self.attributes or {}
        equipment = attributes.get("equipment_ids") or attributes.get("equipment_tags") or []
        equipment_text = ", ".join(str(value) for value in equipment) if isinstance(equipment, list) else str(equipment)
        original_text = self.text if original_text is None else original_text
        return (
            f"Document: {attributes.get('document_title') or Path(self.filename).stem}\n"
            f"Type: {self.document_type}\n"
            f"Equipment: {equipment_text}\n"
            f"Revision: {attributes.get('revision') or ''}\n"
            f"Section: {self.section}\n"
            f"Page: {self.page}\n\n"
            f"{original_text}"
        )


class Citation(BaseModel):
    document_id: uuid.UUID
    filename: str
    page: int
    section: str


class RetrievalResult(BaseModel):
    chunk_id: str
    parent_id: uuid.UUID
    document_id: uuid.UUID
    document_type: str
    project_id: uuid.UUID
    page: int
    section: str
    text: str
    score: float
    dense_rank: int | None = None
    bm25_rank: int | None = None
    rrf_score: float
    citation: Citation
    attributes: dict[str, object] = Field(default_factory=dict)


def file_hash(content: bytes) -> str:
    return sha256(content).hexdigest()


def validate_upload(filename: str, document_type: str, size_bytes: int, settings: Settings) -> None:
    suffix = Path(filename).suffix.lower()
    if document_type not in SUPPORTED_TYPES:
        raise IngestionError("unsupported_document_type", "Unsupported document type")
    if not filename or suffix not in {".pdf", ".csv", ".md", ".txt"}:
        raise IngestionError("unsupported_file", "Only PDF, CSV, Markdown, and text files are supported")
    if document_type == "schedule" and suffix != ".csv":
        raise IngestionError("invalid_schedule", "Schedules must be uploaded as CSV")
    if document_type != "schedule" and suffix == ".csv":
        raise IngestionError("invalid_document", "CSV files must use the schedule document type")
    if size_bytes == 0 or size_bytes > settings.max_upload_bytes:
        raise IngestionError("invalid_file_size", f"File must be between 1 and {settings.max_upload_bytes} bytes")


def extract_document(path: Path, settings: Settings) -> ExtractedDocument:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(path, settings)
    if suffix == ".csv":
        return _extract_schedule(path)
    if suffix in {".md", ".txt"}:
        return _extract_text(path)
    raise IngestionError("unsupported_file", "Unsupported file extension")


def _extract_pdf(path: Path, settings: Settings) -> ExtractedDocument:
    try:
        pdf = fitz.open(path)
    except (fitz.FileDataError, OSError) as exc:
        raise IngestionError("invalid_pdf", "PDF could not be opened") from exc
    try:
        pages = [ExtractedPage(page=index + 1, text=page.get_text("text")) for index, page in enumerate(pdf)]
        metadata = {key: value for key, value in pdf.metadata.items() if value}
        if sum(len(page.text.strip()) for page in pages) < settings.min_pdf_text_chars:
            pages = _ocr_pdf(pdf)
        if not any(page.text.strip() for page in pages):
            raise IngestionError("empty_document", "No text could be extracted from the PDF")
        return ExtractedDocument(pages=pages, metadata=metadata)
    finally:
        pdf.close()


def _ocr_pdf(pdf: fitz.Document) -> list[ExtractedPage]:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise IngestionError("ocr_unavailable", "OCR dependencies are not installed", 503) from exc
    pages: list[ExtractedPage] = []
    try:
        for index, page in enumerate(pdf):
            image = Image.open(io.BytesIO(page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")))
            pages.append(ExtractedPage(page=index + 1, text=pytesseract.image_to_string(image)))
    except Exception as exc:
        raise IngestionError("ocr_failed", "OCR fallback could not extract PDF text", 422) from exc
    return pages


def _extract_text(path: Path) -> ExtractedDocument:
    text = path.read_text(encoding="utf-8", errors="replace")
    pages: list[ExtractedPage] = []
    current_page, current_lines = 1, []
    for line in text.splitlines():
        marker = re.match(r"^## Page (\d+)\s*$", line.strip())
        if marker:
            if current_lines:
                pages.append(ExtractedPage(page=current_page, text="\n".join(current_lines)))
            current_page, current_lines = int(marker.group(1)), []
        else:
            current_lines.append(line)
    if current_lines:
        pages.append(ExtractedPage(page=current_page, text="\n".join(current_lines)))
    if not pages or not any(page.text.strip() for page in pages):
        raise IngestionError("empty_document", "Document contains no text")
    return ExtractedDocument(pages=pages, metadata={"title": _first_heading(text)})


def _extract_schedule(path: Path) -> ExtractedDocument:
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or not rows[0].get("task_id"):
        raise IngestionError("invalid_schedule", "Schedule CSV must contain task_id rows")
    pages = [
        ExtractedPage(
            page=1,
            section=f"Task {row['task_id']}",
            text="\n".join(f"{key}: {value}" for key, value in row.items() if value),
        )
        for row in rows
    ]
    return ExtractedDocument(pages=pages, metadata={"row_count": len(rows), "title": path.stem})


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("# ").strip()
    return None


def extract_metadata(extracted: ExtractedDocument) -> dict[str, object]:
    text = "\n".join(page.text for page in extracted.pages)
    vendor = re.search(r"\*\*Vendor:\*\*\s*([^|(\n]+)", text)
    rfi_status = re.search(r"\*\*Status:\*\*\s*([A-Za-z_ ]+)", text)
    revision_status = re.search(r"\*\*Revision status:\*\*\s*([A-Za-z_ -]+)", text, re.IGNORECASE)
    revision = re.search(r"\*\*Revision:\*\*\s*([A-Za-z0-9._ -]+)", text, re.IGNORECASE)
    equipment = entity_references(text)
    vendor_name = vendor.group(1).strip() if vendor else None
    approval = revision_status.group(1).strip().lower() if revision_status else None
    return {
        **extracted.metadata,
        "page_count": len({page.page for page in extracted.pages}),
        **equipment,
        "equipment_ids": equipment["equipment_tags"],
        "vendor_ids": [vendor_name] if vendor_name else [],
        **({"vendor": vendor_name} if vendor_name else {}),
        **({"rfi_status": rfi_status.group(1).strip().lower()} if rfi_status else {}),
        **({"revision_status": approval, "approval_status": approval} if approval else {}),
        **({"revision": revision.group(1).strip()} if revision else {}),
    }


STATUS_DOCUMENT_TYPES = {"specification", "submittal", "rfi", "change_order"}


def normalize_revision_metadata(document_type: str, metadata: dict[str, object]) -> dict[str, object]:
    """Map document-specific status metadata onto the indexed evidence-currency contract."""
    normalized = dict(metadata)
    kind = document_type.strip().lower().replace("-", "_").replace(" ", "_")
    if kind not in STATUS_DOCUMENT_TYPES:
        return normalized
    generic_status = normalized.get("rfi_status")
    status = normalized.get("revision_status") or normalized.get("approval_status") or generic_status
    if status:
        canonical = " ".join(re.sub(r"[_-]+", " ", str(status).strip().lower()).split())
        normalized["revision_status"] = canonical
        normalized["approval_status"] = canonical
    if kind != "rfi":
        normalized.pop("rfi_status", None)
    return normalized


def entity_references(text: str) -> dict[str, list[str]]:
    return {
        "equipment_tags": sorted(set(EQUIPMENT_PATTERN.findall(text))),
        "spec_references": sorted(set(SPEC_PATTERN.findall(text))),
    }


def chunk_document(
    extracted: ExtractedDocument,
    *,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    document_type: str,
    filename: str,
    max_chars: int = 1_200,
    overlap: int = 160,
    attributes: dict[str, object] | None = None,
) -> list[Chunk]:
    attributes = normalize_revision_metadata(document_type, attributes or {})
    chunks: list[Chunk] = []
    for page in extracted.pages:
        for section, text in _sections(page):
            for part in _windows(text, max_chars, overlap):
                chunks.append(
                    Chunk(
                        project_id=project_id,
                        document_id=document_id,
                        document_type=document_type,
                        filename=filename,
                        page=page.page,
                        section=section,
                        chunk_index=len(chunks),
                        text=part,
                        attributes=attributes,
                        parent_text=text,
                    )
                )
    if not chunks:
        raise IngestionError("empty_document", "Document contains no indexable text")
    return chunks


def _sections(page: ExtractedPage) -> list[tuple[str, str]]:
    if page.section != "General":
        return [(page.section, page.text)]
    sections: list[tuple[str, str]] = []
    title, lines = "General", []
    for line in page.text.splitlines():
        if re.match(r"^#{1,6}\s+", line):
            if lines:
                sections.append((title, "\n".join(lines).strip()))
            title, lines = line.lstrip("# ").strip(), []
        else:
            lines.append(line)
    if lines:
        sections.append((title, "\n".join(lines).strip()))
    return [(title, text) for title, text in sections if text]


def _windows(text: str, max_chars: int, overlap: int) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    lines = text.splitlines()
    if len(lines) > 2 and lines[0].lstrip().startswith("|") and re.match(r"^\s*\|?\s*:?-+", lines[1]):
        header, rows, windows = lines[:2], lines[2:], []
        current = header.copy()
        for row in rows:
            if len("\n".join([*current, row])) > max_chars and len(current) > 2:
                windows.append("\n".join(current))
                current = header.copy()
            current.append(row)
        if len(current) > 2:
            windows.append("\n".join(current))
        return windows
    windows, start = [], 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            boundary = text.rfind("\n", start, end)
            end = boundary if boundary > start + max_chars // 2 else end
        windows.append(text[start:end].strip())
        if end == len(text):
            break
        start = end - overlap
    return [window for window in windows if window]


async def ensure_collection(client: AsyncQdrantClient, settings: Settings) -> None:
    collections = await client.get_collections()
    if settings.qdrant_collection not in {item.name for item in collections.collections}:
        await client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=VectorParams(size=settings.embedding_dimensions, distance=Distance.COSINE),
        )
    else:
        info = await client.get_collection(settings.qdrant_collection)
        vectors = info.config.params.vectors
        if not isinstance(vectors, VectorParams):
            sparse_vectors = info.config.params.sparse_vectors or {}
            if not vectors:
                details = {
                    "collection": settings.qdrant_collection,
                    "expected_vector_configuration": "unnamed",
                    "actual_vector_configuration": "sparse_only" if sparse_vectors else "none",
                    "actual_sparse_vector_names": sorted(sparse_vectors),
                }
            else:
                details = {
                    "collection": settings.qdrant_collection,
                    "expected_vector_configuration": "unnamed",
                    "actual_vector_configuration": "named",
                    "actual_vector_names": sorted(vectors),
                }
            raise IngestionError(
                "embedding_index_mismatch",
                "Qdrant collection must use an unnamed vector configuration",
                500,
                details,
            )
        if vectors.size != settings.embedding_dimensions or vectors.distance != Distance.COSINE:
            raise IngestionError(
                "embedding_index_mismatch",
                "Qdrant collection configuration does not match the configured semantic model",
                500,
                {
                    "collection": settings.qdrant_collection,
                    "expected_dimensions": settings.embedding_dimensions,
                    "actual_dimensions": vectors.size,
                    "expected_distance": Distance.COSINE.value,
                    "actual_distance": vectors.distance.value,
                },
            )
    # Ensure payload indexes exist for all filtered fields (required by Qdrant Cloud)
    indexed_fields = [
        ("project_id", "keyword"),
        ("document_id", "keyword"),
        ("document_type", "keyword"),
        ("record_type", "keyword"),
        ("parent_id", "keyword"),
        ("equipment_tags", "keyword"),
        ("vendor", "keyword"),
        ("rfi_status", "keyword"),
        ("revision_status", "keyword"),
        ("section", "keyword"),
        ("index_version", "keyword"),
    ]
    for field_name, field_schema in indexed_fields:
        try:
            await client.create_payload_index(
                collection_name=settings.qdrant_collection,
                field_name=field_name,
                field_schema=field_schema,
                wait=False,
            )
        except UnexpectedResponse as exc:
            if exc.status_code != 409:
                logger.exception("Unable to create Qdrant payload index", extra={"field_name": field_name})
                raise
            logger.debug("Qdrant payload index already exists", extra={"field_name": field_name})


async def index_chunks(
    client: AsyncQdrantClient,
    embedder: Embedder,
    settings: Settings,
    chunks: list[Chunk],
    *,
    contextual: bool = True,
) -> None:
    await ensure_collection(client, settings)
    for chunk in chunks:
        chunk.attributes = {**(chunk.attributes or {}), "index_version": settings.index_version}
    vectors = await embedder.embed_documents([chunk.contextual_text() if contextual else chunk.text for chunk in chunks])
    if len(vectors) != len(chunks) or any(len(vector) != settings.embedding_dimensions for vector in vectors):
        raise IngestionError("invalid_embedding", "Embedding response did not match the configured dimensions", 502)
    await client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=FilterSelector(filter=document_filter(chunks[0].project_id, chunks[0].document_id)),
        wait=True,
    )
    child_points = [
        PointStruct(
            id=str(uuid.uuid5(chunk.document_id, str(chunk.chunk_index))),
            vector=vector,
            payload=vector_payload(chunk),
        )
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    parents = {str(uuid.uuid5(chunk.document_id, f"{chunk.page}:{chunk.section}")): chunk for chunk in chunks}
    parent_points = [
        PointStruct(
            id=parent_id,
            vector=[0.0] * settings.embedding_dimensions,
            payload=vector_payload(chunk, parent=True),
        )
        for parent_id, chunk in parents.items()
    ]
    await client.upsert(collection_name=settings.qdrant_collection, points=[*child_points, *parent_points], wait=True)


def citation_from_payload(payload: dict[str, object]) -> Citation:
    return Citation(
        document_id=uuid.UUID(str(payload["document_id"])),
        filename=str(payload["filename"]),
        page=int(payload["page"]),
        section=str(payload["section"]),
    )


async def retrieve_chunks(
    client: AsyncQdrantClient,
    embedder: Embedder,
    settings: Settings,
    project_id: uuid.UUID,
    query: str,
    limit: int,
    document_type: str | None = None,
    rfi_status: str | None = None,
    query_plan: "QueryPlan | None" = None,
) -> list[RetrievalResult]:
    vector = (await embedder.embed_queries([query]))[0]
    filters = _retrieval_filters(project_id, document_type, rfi_status, query_plan)
    response = await client.query_points(
        collection_name=settings.qdrant_collection,
        query=vector,
        query_filter=filters,
        limit=settings.dense_retrieval_limit,
        with_payload=True,
    )
    dense = [
        (rank, _payload(point.payload, point.id))
        for rank, point in enumerate((point for point in response.points if (point.score or 0) > 0), start=1)
    ]
    bm25 = _bm25_rank(query, await _filtered_payloads(client, settings, filters))[:settings.bm25_retrieval_limit]
    return _fuse_ranked_candidates(
        dense,
        bm25,
        min(limit, settings.hybrid_retrieval_limit),
        settings.rrf_dense_weight,
        settings.rrf_bm25_weight,
    )


def _retrieval_filters(
    project_id: uuid.UUID,
    document_type: str | None,
    rfi_status: str | None,
    query_plan: "QueryPlan | None",
):
    return retrieval_filter(
        project_id,
        document_type,
        rfi_status,
        document_types=query_plan.document_types if query_plan and not document_type else None,
        document_ids=query_plan.document_ids if query_plan else None,
        equipment_ids=query_plan.equipment_ids if query_plan else None,
        vendor_ids=query_plan.vendor_ids if query_plan else None,
        revision_status=query_plan.revision_status if query_plan else None,
        section=query_plan.section if query_plan else None,
    )


async def _filtered_payloads(client: AsyncQdrantClient, settings: Settings, filters) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    offset = None
    while True:
        points, offset = await client.scroll(
            collection_name=settings.qdrant_collection,
            scroll_filter=filters,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        payloads.extend(_payload(point.payload, point.id) for point in points)
        if offset is None:
            return payloads


async def retrieve_parent_chunks(
    client: AsyncQdrantClient, settings: Settings, project_id: uuid.UUID, parent_id: uuid.UUID
) -> list[RetrievalResult]:
    payloads = await _filtered_payloads(client, settings, parent_filter(project_id, parent_id))
    persisted = [payload for payload in payloads if payload.get("record_type") == "parent"]
    payloads = persisted or payloads
    payloads.sort(key=lambda payload: int(payload.get("chunk_index", 0)))
    return [_retrieval_result(payload, None, None, 0.0) for payload in payloads]


def _payload(payload: dict[str, object] | None, point_id: object) -> dict[str, object]:
    value = dict(payload or {})
    value.setdefault("chunk_id", str(point_id))
    value.setdefault("parent_id", value.get("document_id"))
    return value


TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", re.IGNORECASE)
BM25_STOP_WORDS = {"a", "an", "and", "about", "for", "in", "is", "its", "me", "of", "please", "show", "tell", "that", "the", "this", "to", "what"}


def _bm25_rank(query: str, payloads: list[dict[str, object]]) -> list[tuple[int, dict[str, object]]]:
    terms = [term for term in TOKEN_PATTERN.findall(query.lower()) if term not in BM25_STOP_WORDS]
    if not terms or not payloads:
        return []
    documents = [TOKEN_PATTERN.findall(str(payload.get("contextual_text") or payload.get("text", "")).lower()) for payload in payloads]
    document_frequency = Counter(term for tokens in documents for term in set(tokens))
    average_length = sum(len(tokens) for tokens in documents) / len(documents)
    scored = []
    for payload, tokens in zip(payloads, documents, strict=True):
        counts, score = Counter(tokens), 0.0
        for term in terms:
            frequency = counts[term]
            if not frequency:
                continue
            inverse_frequency = log(1 + (len(documents) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
            score += inverse_frequency * frequency * 2.5 / (frequency + 1.5 * (1 - 0.75 + 0.75 * len(tokens) / max(average_length, 1)))
        if score:
            scored.append((score, payload))
    return [(rank, payload) for rank, (_, payload) in enumerate(sorted(scored, key=lambda item: (-item[0], str(item[1]["chunk_id"]))), start=1)]


def _fuse_ranked_candidates(
    dense: list[tuple[int, dict[str, object]]],
    bm25: list[tuple[int, dict[str, object]]],
    limit: int,
    dense_weight: float = 1.0,
    bm25_weight: float = 1.0,
) -> list[RetrievalResult]:
    candidates: dict[str, dict[str, object]] = {}
    for rank, payload in dense:
        candidates.setdefault(str(payload["chunk_id"]), {"payload": payload, "dense_rank": None, "bm25_rank": None})["dense_rank"] = rank
    for rank, payload in bm25:
        candidates.setdefault(str(payload["chunk_id"]), {"payload": payload, "dense_rank": None, "bm25_rank": None})["bm25_rank"] = rank
    ranked = []
    for chunk_id, item in candidates.items():
        dense_rank, bm25_rank = item["dense_rank"], item["bm25_rank"]
        rrf_score = sum(
            weight / (60 + rank)
            for rank, weight in ((dense_rank, dense_weight), (bm25_rank, bm25_weight))
            if rank is not None
        )
        ranked.append((rrf_score, min(rank for rank in (dense_rank, bm25_rank) if rank is not None), chunk_id, item))
    return [
        _retrieval_result(item["payload"], item["dense_rank"], item["bm25_rank"], rrf_score)
        for rrf_score, _, _, item in sorted(ranked, key=lambda item: (-item[0], item[1], item[2]))[:limit]
    ]


def _retrieval_result(payload: dict[str, object], dense_rank: int | None, bm25_rank: int | None, rrf_score: float) -> RetrievalResult:
    citation = citation_from_payload(payload)
    return RetrievalResult(
        chunk_id=str(payload["chunk_id"]),
        parent_id=uuid.UUID(str(payload["parent_id"])),
        document_id=citation.document_id,
        document_type=str(payload["document_type"]),
        project_id=uuid.UUID(str(payload["project_id"])),
        page=citation.page,
        section=citation.section,
        text=str(payload.get("original_text") or payload["text"]),
        score=rrf_score / (2 / 61),
        dense_rank=dense_rank,
        bm25_rank=bm25_rank,
        rrf_score=rrf_score,
        citation=citation,
        attributes={
            key: value
            for key, value in payload.items()
            if key
            in {
                "document_title",
                "equipment_ids",
                "equipment_tags",
                "vendor_ids",
                "spec_references",
                "rfi_status",
                "vendor",
                "revision",
                "revision_status",
                "approval_status",
                "index_version",
            }
        },
    )


async def run_ingestion(
    session: AsyncSession,
    client: AsyncQdrantClient,
    embedder: Embedder,
    settings: Settings,
    document: Document,
    job: IngestionJob,
    graph_store: "GraphStore | None" = None,
    *,
    allow_completed: bool = False,
) -> IngestionJob:
    owner_token = await _claim_ingestion(session, document, job, allow_completed=allow_completed)
    document_id, job_id = document.id, job.id
    try:
        extracted = extract_document(Path(document.storage_path), settings)
        metadata = normalize_revision_metadata(document.document_type, extract_metadata(extracted))
        metadata["index_version"] = settings.index_version
        metadata["document_title"] = metadata.get("title") or Path(document.filename).stem
        chunks = chunk_document(
            extracted,
            project_id=document.project_id,
            document_id=document.id,
            document_type=document.document_type,
            filename=document.filename,
            attributes={
                key: value
                for key, value in metadata.items()
                if key
                in {
                    "document_title",
                    "equipment_ids",
                    "equipment_tags",
                    "vendor_ids",
                    "spec_references",
                    "rfi_status",
                    "vendor",
                    "revision",
                    "revision_status",
                    "approval_status",
                    "index_version",
                }
            },
        )
        await index_chunks(client, embedder, settings, chunks)
        from app.equipment import sync_document_entities

        await sync_document_entities(session, document, chunks, metadata)
        if graph_store:
            graph_document = copy.copy(document)
            graph_document.metadata_json = metadata
            graph_store.update(graph_document, chunks)
        if not await _complete_ingestion(session, document, job, owner_token, metadata, len(chunks)):
            raise IngestionError("ingestion_ownership_lost", "Ingestion ownership changed before completion", 409)
        logger.info("indexed project=%s document=%s chunks=%s", document.project_id, document.id, len(chunks))
        return job
    except IngestionError as exc:
        await session.rollback()
        if await _mark_failed(session, document_id, job_id, owner_token, exc.message):
            await session.refresh(document)
            await session.refresh(job)
        raise
    except Exception as exc:
        logger.exception("ingestion_failed project=%s document=%s", document.project_id, document.id)
        await session.rollback()
        if await _mark_failed(session, document_id, job_id, owner_token, "Ingestion failed"):
            await session.refresh(document)
            await session.refresh(job)
        raise IngestionError("ingestion_failed", "Ingestion failed", 500) from exc


async def _mark_failed(
    session: AsyncSession,
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    owner_token: uuid.UUID,
    error: str,
) -> bool:
    now = datetime.now(UTC)
    job_result = await session.execute(
        update(IngestionJob)
        .where(
            IngestionJob.id == job_id,
            IngestionJob.status == "processing",
            IngestionJob.owner_token == owner_token,
        )
        .values(status="failed", error=error, completed_at=now, lease_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    document_result = await session.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.status == "processing",
            Document.active_ingestion_job_id == job_id,
            Document.ingestion_owner_token == owner_token,
        )
        .values(
            status="failed",
            active_ingestion_job_id=None,
            ingestion_owner_token=None,
        )
        .execution_options(synchronize_session=False)
    )
    if job_result.rowcount != 1 or document_result.rowcount != 1:
        await session.rollback()
        return False
    await session.commit()
    return True


async def recover_ingestion_attempt(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    job_id: uuid.UUID,
    confirm_worker_stopped: bool,
) -> dict[str, str]:
    if not confirm_worker_stopped:
        raise IngestionError(
            "operator_confirmation_required",
            "Recovery requires confirmation that the owning worker has stopped",
            400,
        )
    if session.get_bind().dialect.name == "sqlite" and not session.in_transaction():
        await session.execute(text("BEGIN IMMEDIATE"))
    document = await session.scalar(
        select(Document).where(Document.id == document_id).with_for_update()
    )
    processing_jobs = list(
        (
            await session.scalars(
                select(IngestionJob)
                .where(
                    IngestionJob.document_id == document_id,
                    IngestionJob.status == "processing",
                )
                .order_by(IngestionJob.attempt_number, IngestionJob.id)
                .with_for_update()
            )
        ).all()
    )
    if document is None or document.status != "processing":
        await session.rollback()
        raise IngestionError(
            "ingestion_recovery_conflict",
            "The selected document is no longer processing",
            409,
        )
    if document.active_ingestion_job_id is not None and document.active_ingestion_job_id != job_id:
        await session.rollback()
        raise IngestionError(
            "ingestion_recovery_conflict",
            "The supplied job is not the document's active processing attempt",
            409,
        )
    if len(processing_jobs) != 1:
        await session.rollback()
        raise IngestionError(
            "ingestion_recovery_ambiguous",
            "Recovery requires exactly one processing job for the document",
            409,
        )
    if processing_jobs[0].id != job_id:
        await session.rollback()
        raise IngestionError(
            "ingestion_recovery_conflict",
            "The supplied job is not the document's sole processing attempt",
            409,
        )
    now = datetime.now(UTC)
    reason = "Operator confirmed the worker stopped; attempt is restartable"
    job_result = await session.execute(
        update(IngestionJob)
        .where(
            IngestionJob.id == job_id,
            IngestionJob.document_id == document_id,
            IngestionJob.status == "processing",
        )
        .values(
            status="failed",
            owner_token=None,
            lease_expires_at=None,
            error=reason,
            completed_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    document_result = await session.execute(
        update(Document)
        .where(
            Document.id == document_id,
            Document.status == "processing",
            or_(
                Document.active_ingestion_job_id == job_id,
                Document.active_ingestion_job_id.is_(None),
            ),
        )
        .values(
            status="failed",
            active_ingestion_job_id=None,
            ingestion_owner_token=None,
        )
        .execution_options(synchronize_session=False)
    )
    if job_result.rowcount != 1 or document_result.rowcount != 1:
        await session.rollback()
        raise IngestionError(
            "ingestion_recovery_conflict",
            "The selected document and job are not the same processing attempt",
            409,
        )
    await session.commit()
    return {"document_id": str(document_id), "job_id": str(job_id), "status": "failed"}


async def reindex_documents(
    session: AsyncSession,
    client: AsyncQdrantClient,
    embedder: Embedder,
    settings: Settings,
    project_id: uuid.UUID,
    document_id: uuid.UUID | None = None,
    *,
    force: bool = False,
) -> dict[str, int]:
    statement = select(Document).where(Document.project_id == project_id)
    if document_id:
        statement = statement.where(Document.id == document_id)
    documents = list((await session.scalars(statement)).all())
    reindexed = skipped = 0
    for document in documents:
        if document.status in {"queued", "processing", "repairing"} or document.active_ingestion_job_id:
            raise IngestionError("ingestion_not_claimed", "An active ingestion attempt already owns this document", 409)
        if not force and (document.metadata_json or {}).get("index_version") == settings.index_version:
            skipped += 1
            continue
        job = IngestionJob(
            project_id=project_id,
            document_id=document.id,
            attempt_number=await next_ingestion_attempt(session, document.id),
            status="queued",
        )
        session.add(job)
        await session.flush()
        await run_ingestion(session, client, embedder, settings, document, job, allow_completed=True)
        reindexed += 1
    return {"matched": len(documents), "reindexed": reindexed, "skipped": skipped}
