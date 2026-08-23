import uuid
from typing import TYPE_CHECKING

from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

if TYPE_CHECKING:
    from app.ingestion import Chunk


def vector_payload(chunk: "Chunk", *, parent: bool = False) -> dict[str, object]:
    chunk_id = str(uuid.uuid5(chunk.document_id, str(chunk.chunk_index)))
    parent_id = str(uuid.uuid5(chunk.document_id, f"{chunk.page}:{chunk.section}"))
    original_text = chunk.parent_text if parent and chunk.parent_text else chunk.text
    attributes = chunk.attributes or {}
    canonical_status = (
        attributes.get("revision_status")
        or attributes.get("approval_status")
        or attributes.get("rfi_status")
        or ""
    )
    payload: dict[str, object] = {
        "chunk_id": parent_id if parent else chunk_id,
        "parent_id": parent_id,
        "record_type": "parent" if parent else "child",
        "project_id": str(chunk.project_id),
        "document_id": str(chunk.document_id),
        "document_type": chunk.document_type,
        "document_title": attributes.get("document_title") or chunk.filename.rsplit(".", 1)[0],
        "filename": chunk.filename,
        "page": chunk.page,
        "section": chunk.section,
        "chunk_index": chunk.chunk_index,
        "original_text": original_text,
        "contextual_text": chunk.contextual_text(original_text),
        "text": original_text,
        "equipment_ids": attributes.get("equipment_ids") or attributes.get("equipment_tags") or [],
        "vendor_ids": attributes.get("vendor_ids") or ([attributes["vendor"]] if attributes.get("vendor") else []),
        "revision": attributes.get("revision") or "",
        "revision_status": canonical_status,
        "approval_status": canonical_status,
        "index_version": attributes.get("index_version") or "1",
    }
    payload.update(attributes)
    payload["revision_status"] = canonical_status
    payload["approval_status"] = canonical_status
    return payload


def project_filter(project_id: uuid.UUID) -> Filter:
    return retrieval_filter(project_id)


def retrieval_filter(
    project_id: uuid.UUID,
    document_type: str | None = None,
    rfi_status: str | None = None,
    *,
    document_types: list[str] | None = None,
    document_ids: list[uuid.UUID] | None = None,
    equipment_ids: list[str] | None = None,
    vendor_ids: list[str] | None = None,
    revision_status: str | None = None,
    section: str | None = None,
) -> Filter:
    conditions = [FieldCondition(key="project_id", match=MatchValue(value=str(project_id)))]
    if document_type:
        conditions.append(FieldCondition(key="document_type", match=MatchValue(value=document_type)))
    elif document_types:
        conditions.append(FieldCondition(key="document_type", match=MatchAny(any=document_types)))
    if document_ids:
        conditions.append(FieldCondition(key="document_id", match=MatchAny(any=[str(value) for value in document_ids])))
    if equipment_ids:
        conditions.append(FieldCondition(key="equipment_tags", match=MatchAny(any=equipment_ids)))
    if vendor_ids:
        conditions.append(FieldCondition(key="vendor", match=MatchAny(any=vendor_ids)))
    if rfi_status:
        conditions.append(FieldCondition(key="rfi_status", match=MatchValue(value=rfi_status)))
    if revision_status:
        conditions.append(FieldCondition(key="revision_status", match=MatchValue(value=revision_status)))
    if section:
        conditions.append(FieldCondition(key="section", match=MatchValue(value=section)))
    return Filter(
        must=conditions,
        must_not=[FieldCondition(key="record_type", match=MatchValue(value="parent"))],
    )


def document_filter(project_id: uuid.UUID, document_id: uuid.UUID) -> Filter:
    return Filter(
        must=[
            FieldCondition(key="project_id", match=MatchValue(value=str(project_id))),
            FieldCondition(key="document_id", match=MatchValue(value=str(document_id))),
        ]
    )


def parent_filter(project_id: uuid.UUID, parent_id: uuid.UUID) -> Filter:
    return Filter(
        must=[
            FieldCondition(key="project_id", match=MatchValue(value=str(project_id))),
            FieldCondition(key="parent_id", match=MatchValue(value=str(parent_id))),
        ]
    )
