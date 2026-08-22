# Atlas Production Reconciliation and Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile both Atlas histories into the canonical production repository, replace hash retrieval with production-safe semantic embeddings, label non-operational surfaces honestly, and verify the result locally and on the deployed application.

**Architecture:** Preserve both Git histories with a merge commit on canonical `main`. Introduce a focused FastEmbed adapter that produces query/passage vectors, migrate retrieval to a new 384-dimensional Qdrant collection, and leave the old collection intact for rollback. Preserve deterministic engineering logic while changing only misleading copy/configuration around advisory routing, synchronous ingestion, synthetic supply chain, and user-input mitigation calculations.

**Tech Stack:** Python 3.11+, FastAPI, FastEmbed 0.8, Qdrant, SQLAlchemy, pytest, Next.js 16, React 19, TypeScript, Vitest, Render, Netlify.

**Spec:** `docs/superpowers/specs/2026-08-22-production-reconciliation-and-retrieval-design.md`

## Global Constraints

- `Prabhav200511/project-Atlas` is the only writable/canonical repository; push each verified commit directly to `origin/main`.
- `nagateja2004/project-Atlas` is read-only; preserve its commits `d27a59f` and `52b7e56` through a true merge and never push to it.
- Never force-push, rebase shared history, squash the source commits, or select one repository wholesale during conflict resolution.
- Do not stage, edit, delete, or move the untracked `Project_Atlas_Detailed_Submission.pdf`.
- Use `BAAI/bge-small-en-v1.5`, `384` dimensions, collection `atlas_chunks_semantic_v1`, and index version `3` exactly.
- No runtime fallback to `LocalHashEmbedder` is permitted.
- Preserve the old 1,536-dimensional Qdrant collection for rollback.
- Do not claim a retrieval improvement unless the checked-in evaluation report measures it.
- Do not implement authentication, a background queue, live logistics integrations, or probabilistic simulation in this scope.
- Every task follows red-green-refactor where behavior changes, ends with verification, commits only its listed files, and pushes immediately to `origin/main`.

## File Structure

- `app/embeddings.py`: owns the semantic embedding protocol and FastEmbed adapter; no Qdrant or HTTP responsibilities.
- `app/ingestion.py`: owns extraction, chunking, Qdrant collection validation, indexing, retrieval, and reindex orchestration.
- `app/config.py`: owns model, dimension, collection, index-version, and cache settings.
- `app/main.py`: constructs one process-wide embedder and reports it in readiness checks.
- `scripts/cache_embedding_model.py`: downloads and validates the configured model during the deployment build.
- `scripts/reindex.py`: explicitly reindexes one project/document into the configured semantic collection.
- `scripts/evaluate_rag.py`: evaluates the same runtime semantic embedder used by the application.
- `tests/test_embeddings.py`: adapter unit tests with an injected fake FastEmbed model.
- `tests/test_ingestion.py`, `tests/test_hybrid_retrieval.py`, `tests/test_index_migration.py`: collection, dimension, index, retrieval, and migration regressions.
- `tests/test_semantic_retrieval.py`: real-model paraphrase acceptance tests.
- `tests/test_query_planning.py`, `tests/test_answer_generation.py`: source-history Copilot fallback regressions and advisory route semantics.
- `frontend/src/components/dashboard.tsx`: visible feature names, badges, and limitation copy.
- `frontend/src/components/dashboard.test.tsx`: rendered truthfulness assertions.
- `.env.example`, `render.yaml`, `DEPLOY.md`: production model/cache/index configuration and removal of unused JWT claims.
- `README.md`, `docs/API.md`, `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE.mermaid`, `docs/DEMO_SCRIPT.md`, `docs/LIMITATIONS.md`, `evaluation/latest.json`, `evaluation/latest.md`: measured behavior and honest capability documentation.

## Execution Preflight

- [ ] Use `superpowers:using-git-worktrees` before Task 1. Create or enter an isolated worktree on `codex/atlas-production-hardening` based on current `origin/main`; the shared working tree containing the untracked PDF must remain untouched.
- [ ] Fetch current canonical state and the source tracking ref:

```powershell
git fetch origin main
git fetch https://github.com/nagateja2004/project-Atlas.git main:refs/remotes/source/main
git rev-parse origin/main
git rev-parse source/main
git rev-list --left-right --count source/main...origin/main
```

Expected before reconciliation: source tip `52b7e56`, production contains design commit `477e4be`, and both sides have unique commits from merge base `2c3592d`.

- [ ] Install current dependencies and run the untouched baseline:

```powershell
python -m pip install -e ".[dev]"
Push-Location frontend
npm ci
Pop-Location
python -m pytest -q
Push-Location frontend
npm test
npm run lint
npm run typecheck
Pop-Location
```

Expected: all pre-change backend/frontend checks pass. If not, stop and report the baseline failure before attributing it to this plan.

---

### Task 1: Reconcile Source Copilot History into Production

**Files:**
- Merge/modify: `app/config.py`
- Merge/modify: `app/llm.py`
- Merge/modify: `app/workflow.py`
- Merge/modify: `frontend/src/lib/api.ts`
- Merge/modify: `tests/test_config.py`
- Merge/modify: `tests/test_llm_gateway.py`
- Merge/modify: `tests/test_query_planning.py`
- Modify: `tests/test_answer_generation.py`

**Interfaces:**
- Consumes: source commits `d27a59f`, `52b7e56`; production Groq-aware `GeminiGateway.is_available`.
- Produces: provider-failure planner fallback and evidence-only `AnswerResult` with valid `SupportingSpan` citations while retaining all production provider/deployment behavior.

- [ ] **Step 1: Start the history-preserving merge without committing**

```powershell
git merge --no-ff --no-commit source/main
git status --short
```

Resolve conflict markers by keeping production Groq/Gemini configuration and gateway behavior. Do not use `--ours` or `--theirs` on whole files.

- [ ] **Step 2: Preserve the source planner regression test**

Ensure `tests/test_query_planning.py` imports `IngestionError` and contains:

```python
@pytest.mark.asyncio
async def test_planner_falls_back_when_model_gateway_is_unavailable() -> None:
    class UnavailableGateway:
        is_available = True

        async def generate(self, *_args, **_kwargs) -> str:
            raise IngestionError("model_gateway_error", "AI provider request failed", 502)

    plan = await GeminiQueryPlanner(Settings(), UnavailableGateway()).plan(
        uuid.uuid4(), "What is UPS-A battery autonomy?", []
    )

    assert plan.standalone_query == "What is UPS-A battery autonomy?"
    assert plan.equipment_ids == ["UPS-A"]
```

- [ ] **Step 3: Add the evidence-fallback regression test**

Append to `tests/test_answer_generation.py` and import `KnowledgeService`:

```python
@pytest.mark.asyncio
async def test_generation_outage_returns_valid_retrieved_evidence() -> None:
    class UnavailableResponder:
        async def generate(self, *_args, **_kwargs) -> object:
            raise IngestionError("generation_unavailable", "AI provider unavailable", 503)

    service = KnowledgeService(Settings(), None, None, responder=UnavailableResponder())
    result = await service._generate_answer("How long is the backup runtime?", context())

    assert result.status == "PARTIAL"
    assert result.citations[0].citation_id == "C1"
    assert result.citations[0].supporting_spans[0].text == "UPS-A battery autonomy shall be 15 minutes."
    assert "[C1]" in result.answer
```

- [ ] **Step 4: Run the focused tests and confirm the production-side conflict resolution is incomplete**

```powershell
python -m pytest tests/test_query_planning.py::test_planner_falls_back_when_model_gateway_is_unavailable tests/test_answer_generation.py::test_generation_outage_returns_valid_retrieved_evidence -q
```

Expected before restoring source behavior: FAIL because `IngestionError` escapes the planner and/or `_generate_answer`.

- [ ] **Step 5: Restore both source fixes in the merged production workflow**

The planner exception list must include `IngestionError`:

```python
        except (IngestionError, ValidationError, ValueError, json.JSONDecodeError):
            return fallback
```

`KnowledgeService._generate_answer` must be:

```python
    async def _generate_answer(self, question: str, context: ContextBundle) -> object:
        generate = getattr(self.responder, "generate", None)
        try:
            return await generate(question, context) if generate else await self.responder.answer(question, context)
        except IngestionError as exc:
            if exc.code in {"generation_unavailable", "model_gateway_error"}:
                return _evidence_fallback(context)
            raise
```

Restore `_evidence_fallback` from source commit `52b7e56`, including conversion of `EvidenceSpan` to `SupportingSpan`:

```python
def _evidence_fallback(context: ContextBundle) -> AnswerResult:
    if not context.chunks:
        return _insufficient_answer(["No retrieved evidence was available."])
    claims: list[AnswerClaim] = []
    citations: list[AnswerCitation] = []
    for index, chunk in enumerate(context.chunks[:3], start=1):
        citation_id = f"C{index}"
        source_span = (chunk.evidence_spans or _fallback_spans(chunk.text))[0]
        span = SupportingSpan(text=source_span.text, start=source_span.start, end=source_span.end)
        claims.append(AnswerClaim(text=span.text, type="fact", citation_ids=[citation_id]))
        citations.append(
            AnswerCitation(
                **chunk.citation.model_dump(),
                citation_id=citation_id,
                chunk_id=chunk.chunk_id,
                supporting_spans=[span],
            )
        )
    return AnswerResult(
        answer="AI generation is unavailable. Retrieved project evidence:\n"
        + "\n".join(f"Document fact: {claim.text} [C{index}]" for index, claim in enumerate(claims, start=1)),
        citations=citations,
        claims=claims,
        confidence=0.5,
        status="PARTIAL",
        missing_information=["AI generation was unavailable; showing retrieved evidence only."],
        conflicting_sources=context.revision_conflicts,
    )
```

- [ ] **Step 6: Verify the reconciled behavior and both ancestry lines**

```powershell
python -m pytest tests/test_query_planning.py tests/test_answer_generation.py tests/test_config.py tests/test_llm_gateway.py -q
python -m pytest -q
git diff --check
git status --short
```

Expected: PASS; only resolved merge files are staged/modified; the submission PDF is absent because execution is in the isolated worktree.

- [ ] **Step 7: Commit the merge and push canonical main**

```powershell
git add app/config.py app/llm.py app/workflow.py frontend/src/lib/api.ts tests/test_config.py tests/test_llm_gateway.py tests/test_query_planning.py tests/test_answer_generation.py
git commit -m "merge: reconcile source copilot fixes into production"
git merge-base --is-ancestor 52b7e56 HEAD
git merge-base --is-ancestor 477e4be HEAD
git push origin HEAD:main
```

Expected: both ancestry commands return exit code `0`; only `Prabhav200511/project-Atlas` advances.

---

### Task 2: Add the Production-Safe FastEmbed Adapter

**Files:**
- Create: `app/embeddings.py`
- Create: `app/errors.py`
- Modify: `app/ingestion.py`
- Modify: `app/config.py`
- Modify: `pyproject.toml`
- Create: `tests/test_embeddings.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: `fastembed.TextEmbedding.passage_embed`, `query_embed`, and `embedding_size`.
- Produces: `Embedder.dimensions`, `embed_documents(list[str])`, `embed_queries(list[str])`, and `FastEmbedder.warmup()`.

- [ ] **Step 1: Write adapter and configuration tests**

Create `tests/test_embeddings.py`:

```python
import pytest

import app.embeddings as embeddings
from app.embeddings import FastEmbedder
from app.ingestion import IngestionError


class FakeModel:
    embedding_size = 3

    def __init__(self, *, model_name: str, cache_dir: str | None = None) -> None:
        self.model_name, self.cache_dir = model_name, cache_dir

    def passage_embed(self, texts: list[str]):
        return iter([[1.0, 0.0, float(index)] for index, _ in enumerate(texts)])

    def query_embed(self, texts: list[str]):
        return iter([[0.0, 1.0, float(index)] for index, _ in enumerate(texts)])


@pytest.mark.asyncio
async def test_fastembed_uses_distinct_passage_and_query_encoders(monkeypatch) -> None:
    monkeypatch.setattr(embeddings, "TextEmbedding", FakeModel)
    embedder = FastEmbedder("test/model", 3, "./cache")

    assert await embedder.embed_documents(["document"]) == [[1.0, 0.0, 0.0]]
    assert await embedder.embed_queries(["question"]) == [[0.0, 1.0, 0.0]]
    assert embedder.dimensions == 3


@pytest.mark.asyncio
async def test_fastembed_rejects_model_dimension_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(embeddings, "TextEmbedding", FakeModel)
    embedder = FastEmbedder("test/model", 384)

    with pytest.raises(IngestionError) as error:
        await embedder.warmup()

    assert error.value.code == "invalid_embedding_configuration"
    assert error.value.details == {"model": "test/model", "expected_dimensions": 384, "actual_dimensions": 3}
```

Add to `tests/test_config.py`:

```python
def test_semantic_embedding_model_and_cache_defaults() -> None:
    settings = Settings()

    assert settings.embedding_model == "BAAI/bge-small-en-v1.5"
    assert settings.embedding_cache_dir == "./.cache/fastembed"
```

- [ ] **Step 2: Run the new tests to verify failure**

```powershell
python -m pytest tests/test_embeddings.py tests/test_config.py::test_semantic_embedding_model_and_cache_defaults -q
```

Expected: FAIL because `app.embeddings` and the model/cache settings do not exist.

- [ ] **Step 3: Extract and extend structured ingestion errors without breaking imports**

Create `app/errors.py`:

```python
class IngestionError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422, details: object | None = None) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(message)
```

In `app/ingestion.py`, replace the class definition with `from app.errors import IngestionError`. Existing `from app.ingestion import IngestionError` imports continue working because the imported name remains in the ingestion module namespace. Keep `LocalHashEmbedder` temporarily so the intermediate canonical commit remains deployable; add `embed_documents` and `embed_queries` wrappers while retaining its old `embed` method until Task 3 migrates all callers.

Also remove the local `Embedder` protocol and import `Embedder` from `app.embeddings`. This is cycle-safe because `app.embeddings` imports only `app.errors`, not `app.ingestion`.

- [ ] **Step 4: Implement `app/embeddings.py`**

```python
import asyncio
from typing import Protocol

from fastembed import TextEmbedding

from app.errors import IngestionError


class Embedder(Protocol):
    dimensions: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_queries(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    def __init__(self, model_name: str, dimensions: int, cache_dir: str | None = None) -> None:
        self.model_name = model_name
        self.dimensions = dimensions
        self.cache_dir = cache_dir
        self._model: TextEmbedding | None = None
        self._load_lock = asyncio.Lock()

    async def warmup(self) -> None:
        await self._get_model()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        model = await self._get_model()
        return await asyncio.to_thread(self._encode_passages, model, texts)

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        model = await self._get_model()
        return await asyncio.to_thread(self._encode_queries, model, texts)

    async def _get_model(self) -> TextEmbedding:
        if self._model is not None:
            return self._model
        async with self._load_lock:
            if self._model is None:
                model = await asyncio.to_thread(
                    TextEmbedding,
                    model_name=self.model_name,
                    cache_dir=self.cache_dir,
                )
                actual = int(model.embedding_size)
                if actual != self.dimensions:
                    raise IngestionError(
                        "invalid_embedding_configuration",
                        "Configured embedding dimensions do not match the model",
                        500,
                        {"model": self.model_name, "expected_dimensions": self.dimensions, "actual_dimensions": actual},
                    )
                self._model = model
        assert self._model is not None
        return self._model

    def _encode_passages(self, model: TextEmbedding, texts: list[str]) -> list[list[float]]:
        return self._vectors(model.passage_embed(texts))

    def _encode_queries(self, model: TextEmbedding, texts: list[str]) -> list[list[float]]:
        return self._vectors(model.query_embed(texts))

    def _vectors(self, values) -> list[list[float]]:
        vectors = [[float(item) for item in value] for value in values]
        if any(len(vector) != self.dimensions for vector in vectors):
            raise IngestionError("invalid_embedding", "Embedding response did not match configured dimensions", 502)
        return vectors
```

- [ ] **Step 5: Add model/cache settings and dependency without changing the active index yet**

In `app/config.py` add:

```python
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str | None = "./.cache/fastembed"
```

Do not change the active `embedding_dimensions`, `qdrant_collection`, or `index_version` in this intermediate commit; the hash runtime must continue using its compatible collection until Task 4 atomically switches runtime and index defaults. In `pyproject.toml`, replace `sentence-transformers>=3.3` with `fastembed>=0.8,<0.9`. The optional cross-encoder already catches an unavailable SentenceTransformers import and production uses `FAST_RERANK=1`; removing the heavy dependency prevents accidental PyTorch deployment.

- [ ] **Step 6: Verify and commit the adapter**

```powershell
python -m pip install -e ".[dev]"
python -m pytest tests/test_embeddings.py tests/test_config.py -q
python -m pytest tests/test_answer_generation.py tests/test_llm_gateway.py -q
python -m compileall -q app
git diff --check
git add app/embeddings.py app/errors.py app/ingestion.py app/config.py pyproject.toml tests/test_embeddings.py tests/test_config.py
git commit -m "feat: add production-safe semantic embeddings"
git push origin HEAD:main
```

---

### Task 3: Make the Qdrant Index Migration Dimension-Safe

**Files:**
- Modify: `app/ingestion.py`
- Modify: `tests/test_ingestion.py`
- Modify: `tests/test_hybrid_retrieval.py`
- Modify: `tests/test_index_migration.py`
- Modify: `scripts/evaluate_synthetic.py`

**Interfaces:**
- Consumes: `Embedder.embed_documents`, `Embedder.embed_queries`, `Settings.embedding_dimensions`.
- Produces: dimension-checked collection creation, passage indexing, query encoding, and actionable `embedding_index_mismatch` errors.

- [ ] **Step 1: Update deterministic test embedders to the new protocol**

For each fake embedder in the three listed test files, replace its single `embed` method with both methods. Example:

```python
class FakeEmbedder:
    dimensions = 2

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]
```

Failing embedders raise the same existing `IngestionError` from both methods. Apply the same two-method protocol to `SyntheticEmbedder` in `scripts/evaluate_synthetic.py`; both methods return its existing deterministic term vector.
After every caller uses the new protocol, remove the temporary `LocalHashEmbedder.embed` compatibility method; keep its document/query methods until Task 4 replaces runtime wiring.

- [ ] **Step 2: Write collection mismatch and encoder-selection tests**

Add to `tests/test_index_migration.py`:

```python
@pytest.mark.asyncio
async def test_existing_collection_dimension_mismatch_is_rejected() -> None:
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    await client.create_collection("semantic", vectors_config=VectorParams(size=2, distance=Distance.COSINE))
    settings = Settings(qdrant_collection="semantic", embedding_dimensions=384)

    with pytest.raises(IngestionError) as error:
        await ensure_collection(client, settings)

    assert error.value.code == "embedding_index_mismatch"
    assert error.value.details == {"collection": "semantic", "expected_dimensions": 384, "actual_dimensions": 2}
```

Add a capturing embedder assertion to `tests/test_hybrid_retrieval.py`:

```python
class CapturingEmbedder:
    dimensions = 2

    def __init__(self) -> None:
        self.document_calls: list[list[str]] = []
        self.query_calls: list[list[str]] = []

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(texts)
        return [[1.0, 0.0] for _ in texts]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        self.query_calls.append(texts)
        return [[1.0, 0.0] for _ in texts]
```

Index one chunk, retrieve one query, then assert `document_calls` contains contextual document text and `query_calls == [[query]]`.

- [ ] **Step 3: Run tests to verify failure**

```powershell
python -m pytest tests/test_index_migration.py::test_existing_collection_dimension_mismatch_is_rejected tests/test_hybrid_retrieval.py -q
```

Expected: FAIL because collection configuration is not inspected and ingestion still calls `embed`.

- [ ] **Step 4: Validate existing collection configuration**

In `ensure_collection`, call `get_collection` when the collection exists, obtain `info.config.params.vectors`, require an unnamed `VectorParams`, and compare `size` and `distance` with `384` and `Distance.COSINE`. Raise:

```python
raise IngestionError(
    "embedding_index_mismatch",
    "Qdrant collection dimensions do not match the configured semantic model",
    500,
    {
        "collection": settings.qdrant_collection,
        "expected_dimensions": settings.embedding_dimensions,
        "actual_dimensions": vectors.size,
    },
)
```

Do not delete or recreate a mismatched collection. Continue creating payload indexes after validation. Catch only the provider's already-exists response for payload indexes; log and re-raise other schema/network errors.

- [ ] **Step 5: Switch indexing and retrieval to the correct encoders**

In `index_chunks`:

```python
vectors = await embedder.embed_documents(
    [chunk.contextual_text() if contextual else chunk.text for chunk in chunks]
)
```

In `retrieve_chunks`:

```python
vector = (await embedder.embed_queries([query]))[0]
```

Retain vector-length checks before Qdrant deletion/upsert, ensuring a bad embedding response cannot erase the document's existing points.

- [ ] **Step 6: Verify migration and retrieval regressions**

```powershell
python -m pytest tests/test_ingestion.py tests/test_hybrid_retrieval.py tests/test_index_migration.py -q
python -m pytest tests/test_knowledge_workflow.py tests/test_context.py -q
git diff --check
```

Expected: PASS, including project isolation, index-version filtering, and mismatch rejection.

- [ ] **Step 7: Commit and push**

```powershell
git add app/ingestion.py scripts/evaluate_synthetic.py tests/test_ingestion.py tests/test_hybrid_retrieval.py tests/test_index_migration.py
git commit -m "fix: make semantic index migration dimension safe"
git push origin HEAD:main
```

---

### Task 4: Wire Runtime, Reindexing, Deployment Cache, and Semantic Acceptance

**Files:**
- Modify: `app/main.py`
- Modify: `scripts/reindex.py`
- Modify: `scripts/evaluate_rag.py`
- Modify: `scripts/seed_demo.py`
- Create: `scripts/cache_embedding_model.py`
- Modify: `.env.example`
- Modify: `.gitignore`
- Modify: `render.yaml`
- Create: `tests/test_semantic_retrieval.py`
- Create: `tests/test_seed_demo.py`

**Interfaces:**
- Consumes: `FastEmbedder`, semantic settings, `reindex_documents`.
- Produces: one runtime semantic embedder, a build-time cache warmer, explicit project reindexing, and real-model paraphrase proof.

- [ ] **Step 1: Write real semantic retrieval acceptance tests**

Create `tests/test_semantic_retrieval.py`:

```python
import uuid

import pytest
from qdrant_client import AsyncQdrantClient

from app.config import Settings
from app.embeddings import FastEmbedder
from app.ingestion import Chunk, index_chunks, retrieve_chunks


@pytest.fixture(scope="session")
def semantic_embedder(tmp_path_factory) -> FastEmbedder:
    settings = Settings(embedding_dimensions=384)
    return FastEmbedder(settings.embedding_model, 384, str(tmp_path_factory.mktemp("fastembed-model")))


@pytest.mark.asyncio
async def test_semantic_query_finds_battery_autonomy_without_keyword_overlap(semantic_embedder) -> None:
    project_id = uuid.uuid4()
    settings = Settings(
        qdrant_collection="semantic_paraphrase",
        embedding_dimensions=384,
        dense_retrieval_limit=5,
        bm25_retrieval_limit=5,
        hybrid_retrieval_limit=5,
    )
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    expected = Chunk(project_id, uuid.uuid4(), "specification", "UPS_Specification.md", 2, "Battery", 0, "UPS-A battery autonomy shall be 15 minutes.")
    distractor = Chunk(project_id, uuid.uuid4(), "specification", "Switchgear.md", 1, "Access", 0, "Switchgear rear clearance shall be 900 mm.")

    await index_chunks(client, semantic_embedder, settings, [expected, distractor])
    results = await retrieve_chunks(client, semantic_embedder, settings, project_id, "How long does backup power last during an outage?", 5)

    assert results[0].document_id == expected.document_id


@pytest.mark.asyncio
async def test_semantic_rfi_search_finds_previous_answer_by_meaning(semantic_embedder) -> None:
    project_id = uuid.uuid4()
    settings = Settings(qdrant_collection="semantic_rfi", embedding_dimensions=384)
    client = AsyncQdrantClient(location=":memory:", check_compatibility=False)
    previous = Chunk(
        project_id,
        uuid.uuid4(),
        "RFI",
        "RFI-003.md",
        1,
        "General",
        0,
        "The UPS bypass cabinet may be maintained from the front. **Answer:** Provide 900 mm service clearance.",
        attributes={"rfi_status": "answered"},
    )
    await index_chunks(client, semantic_embedder, settings, [previous])

    results = await retrieve_chunks(
        client,
        semantic_embedder,
        settings,
        project_id,
        "Is one-sided technician servicing permitted?",
        5,
        "RFI",
        "answered",
    )

    assert results and results[0].document_id == previous.document_id
```

- [ ] **Step 2: Run the semantic tests before runtime wiring**

```powershell
python -m pytest tests/test_semantic_retrieval.py -q
```

Expected: the model downloads once; tests expose any adapter/model/query-passage integration defect before application wiring.

- [ ] **Step 3: Wire the semantic embedder everywhere**

In `app/main.py`:

```python
from app.embeddings import FastEmbedder

app.state.embedder = FastEmbedder(
    settings.embedding_model,
    settings.embedding_dimensions,
    settings.embedding_cache_dir,
)
```

Atomically change `Settings` defaults to `embedding_dimensions=384`, `qdrant_collection="atlas_chunks_semantic_v1"`, and `index_version="3"`. Extend `test_semantic_embedding_model_and_cache_defaults` with assertions for those three exact values. Update `scripts/reindex.py` and `scripts/evaluate_rag.py` to construct the same `FastEmbedder`. The fake embedders and `scripts/evaluate_synthetic.py` were migrated in Task 3. Remove `LocalHashEmbedder`, `_hash_embedding`, `hashlib`, and `math` from `app/ingestion.py`. Confirm removal with:

```powershell
rg -n "LocalHashEmbedder|async def embed\(" app scripts tests
```

Expected: no matches.

Add `--reupload` to `scripts/seed_demo.py`. When set, do not skip filenames returned by the document listing; POST every synthetic source so the API's existing lost-file duplicate cleanup can repair ephemeral Render uploads and index them into version `3`. Without the flag, retain the current idempotent skip behavior. Extract this decision:

```python
def should_upload(filename: str, existing_files: set[str], reupload: bool) -> bool:
    return reupload or filename not in existing_files
```

Create `tests/test_seed_demo.py`:

```python
from scripts.seed_demo import should_upload


def test_reupload_includes_existing_documents() -> None:
    existing = {"UPS_Specification.md"}

    assert should_upload("UPS_Specification.md", existing, False) is False
    assert should_upload("UPS_Specification.md", existing, True) is True
```

- [ ] **Step 4: Add the build-time model warmer**

Create `scripts/cache_embedding_model.py`:

```python
"""Download and validate the configured semantic embedding model for deployment."""

import asyncio

from app.config import get_settings
from app.embeddings import FastEmbedder


async def warm() -> None:
    settings = get_settings()
    embedder = FastEmbedder(settings.embedding_model, settings.embedding_dimensions, settings.embedding_cache_dir)
    await embedder.warmup()
    print(f"cached {settings.embedding_model} ({settings.embedding_dimensions} dimensions)")


if __name__ == "__main__":
    asyncio.run(warm())
```

Add `.cache/` to `.gitignore`. Add the five semantic environment variables to `.env.example`. Change Render build command to:

```yaml
buildCommand: pip install . && python scripts/cache_embedding_model.py
```

Set explicit Render values for model, dimensions, cache directory, collection, and index version; remove `JWT_SECRET_KEY` in Task 5, not here.

- [ ] **Step 5: Verify model cache, focused tests, and full backend**

```powershell
python scripts/cache_embedding_model.py
python -m pytest tests/test_embeddings.py tests/test_semantic_retrieval.py tests/test_ingestion.py tests/test_hybrid_retrieval.py tests/test_index_migration.py -q
python -m pytest -q
python -m compileall -q app scripts evaluation migrations
git diff --check
```

Expected: all pass; cache warming prints the exact model and `384` dimensions.

- [ ] **Step 6: Run the baseline-versus-advanced evaluation**

```powershell
python scripts/evaluate_rag.py
python -m evaluation.run_all
```

Inspect `evaluation/latest.json`. Required before commit: advanced Recall@12 `>= 1.0`, advanced correct-document rate `> 0.0`, advanced citation precision `> 0.0`, and both semantic tests pass. If a requirement fails, do not weaken the threshold or claim success; diagnose the retrieval/answer pipeline under `superpowers:systematic-debugging` and rerun this step.

- [ ] **Step 7: Commit runtime wiring and acceptance proof**

```powershell
git add app/main.py app/config.py app/ingestion.py scripts/reindex.py scripts/evaluate_rag.py scripts/seed_demo.py scripts/cache_embedding_model.py .env.example .gitignore render.yaml tests/test_config.py tests/test_semantic_retrieval.py tests/test_seed_demo.py
git commit -m "test: prove semantic retrieval and RFI paraphrase matching"
git push origin HEAD:main
```

Do not stage `evaluation/latest.*` yet; measured documentation is Task 6.

---

### Task 5: Label Demo, Advisory, and Synchronous Surfaces Honestly

**Files:**
- Modify: `app/config.py`
- Modify: `app/workflow.py`
- Modify: `.env.example`
- Modify: `render.yaml`
- Modify: `DEPLOY.md`
- Modify: `frontend/src/components/dashboard.tsx`
- Modify: `frontend/src/components/dashboard.test.tsx`
- Modify: `tests/test_query_planning.py`
- Modify: `docs/API.md`
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/ARCHITECTURE.mermaid`
- Modify: `docs/DEMO_SCRIPT.md`
- Modify: `docs/LIMITATIONS.md`

**Interfaces:**
- Consumes: current deterministic mitigation/supply workflows and `QueryPlanResult`.
- Produces: `QueryPlanResult.execution_mode == "advisory_only"` and user-visible labels that match actual behavior.

- [ ] **Step 1: Write failing API and rendered UI truthfulness tests**

Add to `tests/test_query_planning.py`:

```python
@pytest.mark.asyncio
async def test_route_plan_is_explicitly_advisory_only() -> None:
    project_id = uuid.uuid4()
    route = await KnowledgeService(Settings(), None, None).route_query(project_id, "Show critical path risk", [])

    assert route.execution_mode == "advisory_only"
    assert route.endpoint == f"/projects/{project_id}/schedule/analysis"
```

Update the dashboard destination test to require:

```typescript
for (const label of ["Mitigation calculator", "Synthetic supply-chain demo"]) expect(html).toContain(label);
for (const misleading of ["Mitigation simulator", "Supply-chain simulation"]) expect(html).not.toContain(misleading);
expect(html).toContain("User-supplied assumptions");
expect(html).toContain("No live carrier, AIS, vendor, ERP, or position feed");
```

- [ ] **Step 2: Run focused tests to verify failure**

```powershell
python -m pytest tests/test_query_planning.py::test_route_plan_is_explicitly_advisory_only -q
Push-Location frontend
npm test -- dashboard.test.tsx
Pop-Location
```

Expected: FAIL on missing advisory mode and old labels.

- [ ] **Step 3: Make routing semantics explicit**

Change `QueryPlanResult` in `app/workflow.py`:

```python
class QueryPlanResult(BaseModel):
    plan: QueryPlan
    service: str
    endpoint: str
    execution_mode: Literal["advisory_only"] = "advisory_only"
```

Keep service/endpoint as suggested destinations; do not imply `project_copilot` executed them. Update API/architecture documentation from “routes” or “dispatches” to “classifies and recommends an existing endpoint; Copilot continues through knowledge retrieval.”

- [ ] **Step 4: Rename and disclose UI capabilities**

In `dashboard.tsx`:

- Navigation: `Mitigation calculator`, `Synthetic supply-chain demo`.
- Heading: `Counterfactual mitigation calculator`.
- Calculator copy begins `User-supplied assumptions are applied deterministically; values are not predictions or quotations.`
- Supply heading: `Synthetic supply-chain demo`.
- Supply copy includes exactly `No live carrier, AIS, vendor, ERP, or position feed is connected.`
- Keep `demo_mock`/synthetic badges visible and change `SyntheticBadge` text to `Synthetic demo data`.
- Documents upload copy states `Files are parsed and indexed synchronously in this request; no background worker or retry queue is running.`

Do not remove working deterministic APIs or persisted records.

- [ ] **Step 5: Remove unused JWT claims/configuration**

Delete `jwt_secret_key` from `Settings`, `JWT_SECRET_KEY=` from `.env.example`, the Render JWT variable, and JWT references in `DEPLOY.md`. Retain the authenticated-gateway warning and explicit lack of application authentication/RBAC in `docs/LIMITATIONS.md`.

- [ ] **Step 6: Update demo and architecture wording**

Update `docs/DEMO_SCRIPT.md` labels and narration. In `docs/ARCHITECTURE.mermaid`, rename `Router` to `Planner` and use dashed/advisory edges to suggested services; the solid Copilot flow continues to retrieval. Update `docs/API.md` to say ingestion is synchronous and `/query-plan` is advisory only.

- [ ] **Step 7: Verify backend, frontend, and documentation consistency**

```powershell
python -m pytest tests/test_query_planning.py tests/test_config.py tests/test_dashboard_api.py -q
Push-Location frontend
npm test
npm run lint
npm run typecheck
npm run build
Pop-Location
rg -n "JWT_SECRET_KEY|jwt_secret_key|Mitigation simulator|Supply-chain simulation" app frontend .env.example render.yaml DEPLOY.md docs README.md
git diff --check
```

Expected: tests/build pass. The final search returns no active product/config claims; historical spec/plan mentions are allowed and must not be edited.

- [ ] **Step 8: Commit and push**

```powershell
git add app/config.py app/workflow.py .env.example render.yaml DEPLOY.md frontend/src/components/dashboard.tsx frontend/src/components/dashboard.test.tsx tests/test_query_planning.py docs/API.md docs/ARCHITECTURE.md docs/ARCHITECTURE.mermaid docs/DEMO_SCRIPT.md docs/LIMITATIONS.md
git commit -m "fix: label demo and advisory product surfaces honestly"
git push origin HEAD:main
```

---

### Task 6: Complete Local and Production Verification and Publish Measurements

**Files:**
- Modify: `evaluation/latest.json`
- Modify: `evaluation/latest.md`
- Modify: `docs/LIMITATIONS.md`
- Modify: `README.md`
- Modify: `IMPLEMENTATION_STATUS.md`
- Modify: `FINAL_STATUS.md`

**Interfaces:**
- Consumes: completed semantic runtime, evaluation harness, deployed Render API, deployed Netlify UI.
- Produces: reproducible measured results and verified deployment status with no unsupported claims.

- [ ] **Step 1: Run the final local automated gate from a clean status**

```powershell
git status --short
python -m pytest -q
python -m compileall -q app scripts evaluation migrations
python -m evaluation.run_all
Push-Location frontend
npm test
npm run lint
npm run typecheck
npm run build
Pop-Location
git diff --check
```

Expected: all checks pass. Only measured reports/docs may be modified after `evaluation.run_all`.

- [ ] **Step 2: Start and smoke-test local dependencies and application**

```powershell
docker compose up -d postgres qdrant
alembic upgrade head
python scripts/cache_embedding_model.py
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

In a second terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-RestMethod http://127.0.0.1:8001/ready
$seedOutput = python scripts/seed_demo.py --api-url http://127.0.0.1:8001 --project-name "Atlas Semantic Local Smoke"
$smokeProjectId = [regex]::Match(($seedOutput -join " "), 'Seeded project ([0-9a-f-]+)').Groups[1].Value
if (-not $smokeProjectId) { throw "Seed output did not contain a project ID" }
python scripts/reindex.py --project-id $smokeProjectId --force
```

POST a Copilot paraphrase (`How long does backup power last during an outage?`) and an RFI paraphrase (`Is one-sided technician servicing permitted?`) to `$smokeProjectId`. Required: HTTP 200, non-empty citations, expected UPS/RFI documents, and no hash fallback logs.

- [ ] **Step 3: Record exact local measurements**

Regenerate `evaluation/latest.json` and `.md`. Update `docs/LIMITATIONS.md`, `README.md`, `IMPLEMENTATION_STATUS.md`, and `FINAL_STATUS.md` with the exact generated advanced/baseline values. Remove the old `0.0` statements only if the new report disproves them; otherwise preserve and explain them.

- [ ] **Step 4: Wait for and verify the Task 5 Render deployment**

Poll at reasonable intervals until the deployed commit is live:

```powershell
Invoke-RestMethod https://project-atlas-rd7v.onrender.com/health
Invoke-RestMethod https://project-atlas-rd7v.onrender.com/ready
```

Required: `/health` is `200`; `/ready` is `200` with database and Qdrant `ok`. If startup fails, inspect Render logs, diagnose under `superpowers:systematic-debugging`, add a focused regression where possible, commit/push the fix, and repeat.

- [ ] **Step 5: Seed/reindex the deployed semantic collection**

From the local repository, run the idempotent production seed against the existing synthetic demo project:

```powershell
$productionSeedOutput = python scripts/seed_demo.py --api-url https://project-atlas-rd7v.onrender.com --project-name "Atlas Synthetic Demo" --reupload
$productionProjectId = [regex]::Match(($productionSeedOutput -join " "), 'Seeded project ([0-9a-f-]+)').Groups[1].Value
if (-not $productionProjectId) { throw "Production seed output did not contain a project ID" }
```

Re-uploading is expected to repair document records whose old ephemeral storage paths disappeared during deploy. It must not change stable project/document relationships beyond the existing duplicate-cleanup behavior. Preserve the old Qdrant collection.

- [ ] **Step 6: Execute deployed functional smoke tests**

Against `$productionProjectId`:

- POST `/projects/{id}/copilot` with `How long does backup power last during an outage?`; require `200`, `PARTIAL` or `ANSWERED`, and at least one valid citation to the UPS specification.
- POST `/projects/{id}/rfis/matches` with `Is one-sided technician servicing permitted?`; require a previous answered RFI match.
- POST `/projects/{id}/query-plan`; require `execution_mode: advisory_only`.
- Open `https://project-atlas.netlify.app`; require `Mitigation calculator`, `Synthetic supply-chain demo`, the no-live-feed disclosure, and working API-origin requests.
- Confirm no old misleading nav labels remain and frontend assets return `200`.

- [ ] **Step 7: Publish measured local and production results**

Update `evaluation/latest.json`, `evaluation/latest.md`, `docs/LIMITATIONS.md`, `README.md`, `IMPLEMENTATION_STATUS.md`, and `FINAL_STATUS.md` with the exact local metrics and the dated production smoke outcomes from Steps 4–6. Do not describe a production check as passing unless its HTTP/UI evidence was observed.

```powershell
git add evaluation/latest.json evaluation/latest.md docs/LIMITATIONS.md README.md IMPLEMENTATION_STATUS.md FINAL_STATUS.md
git commit -m "docs: publish measured retrieval and deployment results"
git push origin HEAD:main
git status --short
```

Expected: clean isolated worktree and canonical `main` advanced to the documentation commit.

- [ ] **Step 8: Final ancestry and repository checks**

```powershell
git fetch origin main
git merge-base --is-ancestor 52b7e56 origin/main
git merge-base --is-ancestor 477e4be origin/main
git rev-parse HEAD
git rev-parse origin/main
git ls-remote https://github.com/nagateja2004/project-Atlas.git refs/heads/main
```

Expected: both ancestry checks return `0`; local hardening tip equals `origin/main`; the read-only source tip remains `52b7e56`.

- [ ] **Step 9: Complete verification handoff**

Invoke `superpowers:verification-before-completion`, capture the fresh command outputs, and report:

- final canonical commit SHA;
- source and production ancestry preservation;
- backend/frontend/evaluation totals;
- local smoke evidence;
- deployed health/readiness/Copilot/RFI/UI evidence;
- exact measured RAG values;
- any residual limitation that remains explicitly labelled.
