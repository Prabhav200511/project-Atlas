import asyncio
import logging
import math
import os
import re
import uuid
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from pydantic import BaseModel, Field

from app.config import Settings
from app.ingestion import RetrievalResult

logger = logging.getLogger("atlas.context")
WORD = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", re.IGNORECASE)
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
STOP_WORDS = {"a", "an", "and", "are", "for", "from", "in", "is", "of", "on", "or", "the", "to", "what", "with"}
APPROVED_REVISIONS = {"approved", "current", "ifc", "issued for bid", "issued for construction", "answered"}


class EvidenceSpan(BaseModel):
    start: int
    end: int
    text: str


class ContextChunk(RetrievalResult):
    rerank_score: float
    evidence_spans: list[EvidenceSpan] = Field(default_factory=list)
    expanded_from_chunk_ids: list[str] = Field(default_factory=list)


class RevisionConflict(BaseModel):
    document_key: str
    section: str
    document_ids: list[uuid.UUID]
    revisions: list[str]


class ContextBundle(BaseModel):
    project_id: uuid.UUID
    query: str
    chunks: list[ContextChunk]
    revision_conflicts: list[RevisionConflict] = Field(default_factory=list)
    total_tokens: int
    max_context_tokens: int
    sufficient: bool = True
    sufficiency_reasons: list[str] = Field(default_factory=list)
    retrieval_attempts: int = 1
    corrective_query: str | None = None


class Reranker(Protocol):
    async def score(self, query: str, texts: list[str]) -> list[float]: ...


@lru_cache(maxsize=2)
def _cross_encoder(model_name: str):
    os.environ.setdefault("USE_TF", "0")
    os.environ.setdefault("USE_FLAX", "0")
    from sentence_transformers import CrossEncoder

    return CrossEncoder(model_name)


def _predict(model_name: str, query: str, texts: list[str]):
    return _cross_encoder(model_name).predict([[query, text] for text in texts])


class CrossEncoderReranker:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    async def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        if os.getenv("FAST_RERANK", "0").lower() in ("1", "true", "yes"):
            return LexicalReranker().score_sync(query, texts)
        try:
            scores = await asyncio.wait_for(asyncio.to_thread(_predict, self.model_name, query, texts), timeout=8.0)
            return [round(_normalize_score(float(score)), 6) for score in scores]
        except (asyncio.TimeoutError, TimeoutError, Exception) as exc:
            logger.warning("cross_encoder_unavailable model=%s error=%s (falling back to LexicalReranker)", self.model_name, type(exc).__name__)
            return LexicalReranker().score_sync(query, texts)


class LexicalReranker:
    async def score(self, query: str, texts: list[str]) -> list[float]:
        return self.score_sync(query, texts)

    def score_sync(self, query: str, texts: list[str]) -> list[float]:
        query_terms = _terms(query)
        if not query_terms:
            return [0.0] * len(texts)
        return [round(len(query_terms & _terms(text)) / len(query_terms), 6) for text in texts]


ParentLoader = Callable[[uuid.UUID, uuid.UUID], Awaitable[list[RetrievalResult]]]


class PostRetrievalProcessor:
    def __init__(
        self,
        settings: Settings,
        reranker: Reranker | None = None,
        parent_loader: ParentLoader | None = None,
    ) -> None:
        self.settings = settings
        self.reranker = reranker or CrossEncoderReranker(settings.reranker_model)
        self.parent_loader = parent_loader

    async def process(
        self, query: str, project_id: uuid.UUID, results: list[RetrievalResult]
    ) -> ContextBundle:
        selected, conflicts = await self.rerank(query, project_id, results)
        expanded = await self.expand(selected)
        return self.compress(query, project_id, expanded, conflicts)

    async def rerank(
        self, query: str, project_id: uuid.UUID, results: list[RetrievalResult]
    ) -> tuple[list[tuple[RetrievalResult, float]], list[RevisionConflict]]:
        scoped = [item for item in results if item.project_id == project_id]
        scores = await self.reranker.score(query, [item.text for item in scoped])
        ranked = sorted(
            zip(scoped, scores, strict=True),
            key=lambda item: (-_revision_priority(item[0]), -item[1], -item[0].rrf_score, item[0].chunk_id),
        )
        conflicts, allowed = _revision_conflicts(ranked)
        selected = _select_diverse(
            [item for item in ranked if item[0].chunk_id in allowed],
            self.settings.reranker_score_threshold,
            self.settings.context_min_chunks,
            self.settings.context_max_chunks,
            self.settings.context_diversity_threshold,
        )
        return selected, conflicts

    async def expand(
        self, selected: list[tuple[RetrievalResult, float]]
    ) -> list[tuple[RetrievalResult, float, str, list[str]]]:
        expanded = []
        for item, rerank_score in selected:
            source, expanded_ids = await self._expand(item)
            expanded.append((item, rerank_score, source, expanded_ids))
        return expanded

    def compress(
        self,
        query: str,
        project_id: uuid.UUID,
        expanded: list[tuple[RetrievalResult, float, str, list[str]]],
        conflicts: list[RevisionConflict],
    ) -> ContextBundle:
        chunks, seen, remaining = [], [], self.settings.max_context_tokens
        for item, rerank_score, source, expanded_ids in expanded:
            text, spans = _compress(query, source, seen)
            text, spans = _fit_budget(text, spans, remaining)
            if not text:
                continue
            token_count = _token_count(text)
            remaining -= token_count
            data = item.model_dump()
            data["text"] = text
            chunks.append(
                ContextChunk(
                    **data,
                    rerank_score=rerank_score,
                    evidence_spans=spans,
                    expanded_from_chunk_ids=expanded_ids,
                )
            )
            seen.extend(_segments(text))
            if remaining <= 0:
                break
        return ContextBundle(
            project_id=project_id,
            query=query,
            chunks=chunks,
            revision_conflicts=conflicts,
            total_tokens=sum(_token_count(chunk.text) for chunk in chunks),
            max_context_tokens=self.settings.max_context_tokens,
        )

    async def _expand(self, item: RetrievalResult) -> tuple[str, list[str]]:
        if not self.parent_loader or not _needs_parent(item):
            return item.text, []
        siblings = await self.parent_loader(item.project_id, item.parent_id)
        if not siblings:
            return item.text, []
        text = "\n".join(dict.fromkeys([*(sibling.text for sibling in siblings), item.text]))
        return text, [sibling.chunk_id for sibling in siblings if sibling.chunk_id != item.chunk_id]


def _select_diverse(
    ranked: list[tuple[RetrievalResult, float]], threshold: float, minimum: int, maximum: int, diversity: float
) -> list[tuple[RetrievalResult, float]]:
    selected: list[tuple[RetrievalResult, float]] = []
    per_document: defaultdict[uuid.UUID, int] = defaultdict(int)
    for item, score in ranked:
        if score < threshold or per_document[item.document_id] >= 2:
            continue
        if any(_similarity(item.text, chosen.text) >= diversity for chosen, _ in selected):
            continue
        selected.append((item, score))
        per_document[item.document_id] += 1
        if len(selected) == maximum:
            return selected
    for candidate in ranked:
        if candidate not in selected:
            selected.append(candidate)
        if len(selected) >= min(minimum, maximum):
            break
    return selected[:maximum]


def _revision_conflicts(
    ranked: list[tuple[RetrievalResult, float]],
) -> tuple[list[RevisionConflict], set[str]]:
    groups: defaultdict[tuple[str, str], list[RetrievalResult]] = defaultdict(list)
    for item, _ in ranked:
        groups[(_document_key(item.citation.filename), item.section.lower())].append(item)
    allowed = {item.chunk_id for item, _ in ranked}
    conflicts = []
    for (document_key, section), items in groups.items():
        revisions = {_revision_label(item) for item in items}
        texts = {" ".join(item.text.lower().split()) for item in items}
        documents = {item.document_id for item in items}
        if len(documents) < 2 or len(revisions) < 2 or len(texts) < 2:
            continue
        conflicts.append(
            RevisionConflict(
                document_key=document_key,
                section=section,
                document_ids=sorted(documents, key=str),
                revisions=sorted(revisions),
            )
        )
        approved = [item for item in items if _revision_priority(item)]
        if approved:
            allowed.difference_update(item.chunk_id for item in items if not _revision_priority(item))
    return conflicts, allowed


def _compress(query: str, source: str, seen: list[str]) -> tuple[str, list[EvidenceSpan]]:
    query_terms = _terms(query)
    pieces = _segments(source)
    relevant = []
    for index, piece in enumerate(pieces):
        heading = piece.lstrip().startswith("#")
        if heading or _terms(piece) & query_terms or _identifiers(piece) & _identifiers(query):
            if not any(_similarity(piece, previous) >= 0.92 for previous in seen + relevant):
                relevant.append(piece)
        elif piece.startswith("|") and index + 1 < len(pieces) and _terms(pieces[index + 1]) & query_terms:
            relevant.append(piece)
    if not relevant and pieces:
        relevant = [max(pieces, key=lambda piece: len(_terms(piece) & query_terms))]
    text = "\n".join(relevant).strip()
    spans, cursor = [], 0
    for piece in relevant:
        start = source.find(piece, cursor)
        if start < 0:
            start = source.find(piece)
        if start >= 0:
            spans.append(EvidenceSpan(start=start, end=start + len(piece), text=piece))
            cursor = start + len(piece)
    return text, spans


def _fit_budget(text: str, spans: list[EvidenceSpan], budget: int) -> tuple[str, list[EvidenceSpan]]:
    if budget <= 0:
        return "", []
    kept, used = [], 0
    for piece in _segments(text):
        cost = _token_count(piece)
        if used + cost > budget:
            break
        kept.append(piece)
        used += cost
    result = "\n".join(kept).strip()
    kept_text = set(kept)
    return result, [span for span in spans if span.text in kept_text]


def _needs_parent(item: RetrievalResult) -> bool:
    text = item.text.strip()
    has_heading = text.startswith("#") or item.section.lower() in text[:200].lower()
    incomplete_start = bool(text and text[0].islower())
    incomplete_table = text.count("|") and "---" not in text
    return not has_heading or incomplete_start or bool(incomplete_table)


def _revision_priority(item: RetrievalResult) -> int:
    return int(str(item.attributes.get("revision_status", "")).strip().lower() in APPROVED_REVISIONS)


def _revision_label(item: RetrievalResult) -> str:
    return str(item.attributes.get("revision") or item.attributes.get("revision_status") or "unspecified").strip().lower()


def _document_key(filename: str) -> str:
    stem = Path(filename).stem.lower()
    return re.sub(r"(?:[_ -](?:rev(?:ision)?)[_ -]?[a-z0-9.]+)$", "", stem)


def _segments(text: str) -> list[str]:
    return [piece.strip() for piece in SENTENCE.split(text) if piece.strip()]


def _terms(text: str) -> set[str]:
    return {term for term in WORD.findall(text.lower()) if term not in STOP_WORDS}


def _identifiers(text: str) -> set[str]:
    return {term for term in WORD.findall(text.lower()) if any(character.isdigit() for character in term)}


def _similarity(left: str, right: str) -> float:
    left_terms, right_terms = _terms(left), _terms(right)
    union = left_terms | right_terms
    return len(left_terms & right_terms) / len(union) if union else 0.0


def _token_count(text: str) -> int:
    return math.ceil(len(text) / 4)


def _normalize_score(score: float) -> float:
    return score if 0 <= score <= 1 else 1 / (1 + math.exp(-score))
