# Atlas Production Reconciliation and Retrieval Design

## Objective

Make `Prabhav200511/project-Atlas` the sole canonical repository, preserve all useful history and behavior from `nagateja2004/project-Atlas`, replace the non-semantic hash retriever with a production-safe semantic embedder, and make every demo-only or advisory product surface explicit. Each independently verified milestone is committed and pushed directly to canonical `main`. The read-only source repository is never pushed or rewritten.

## Current State

The repositories share commit `2c3592d` and then diverge:

- `nagateja2004/project-Atlas` has two unique Copilot resilience commits: `d27a59f` and `52b7e56`.
- `Prabhav200511/project-Atlas` has eleven unique commits through `4815d57`, including Render/Netlify configuration, Qdrant indexes, Groq support, ingestion cleanup, and submission documentation.
- The local checkout tracks the canonical production repository and contains one untracked generated artifact, `Project_Atlas_Detailed_Submission.pdf`. That file must not be overwritten, deleted, or accidentally committed unless separately requested.
- Runtime retrieval still uses `LocalHashEmbedder` with 1,536-dimensional lexical hash vectors. The existing production reranker is intentionally lexical because loading a PyTorch cross-encoder caused Render download and memory failures.
- The current advanced RAG report records `0.0` correct-document rate, correct-page rate, and citation precision.

## Repository Reconciliation

Canonical `main` will merge the fetched `source/main` history with a true merge commit. It will not squash, rebase, force-push, or copy files over the production tree. This retains the identities of all source and production commits.

Conflicts must be resolved by behavior, not by selecting one side wholesale:

- Keep production Groq support, Gemini support, current deployment configuration, Qdrant payload indexes, ingestion cleanup, README deployment links, and submission assets.
- Restore source-side query-planner fallback when an AI provider raises `IngestionError`.
- Restore the evidence-only Copilot fallback when generation is unavailable.
- Preserve the source fix that creates valid `SupportingSpan` values in fallback citations.
- Restore focused regression tests for the provider-unavailable planner and evidence fallback, adapting them to the current gateway interface.
- Keep the production frontend API URL behavior unless a source-side change is demonstrably required by local or deployed routing tests.

After focused and full regression tests pass, push the merge commit only to `origin/main` (`Prabhav200511/project-Atlas`). Confirm that `origin/main` contains both divergent histories using `git merge-base --is-ancestor` for the source and prior production tips.

## Semantic Embedding Architecture

### Runtime model

Use Qdrant FastEmbed with `BAAI/bge-small-en-v1.5` as the runtime semantic model. It produces 384-dimensional vectors from a quantized ONNX model and avoids loading PyTorch. This satisfies the functional recommendation in the assessment without reversing the existing Render memory fix.

Create `app/embeddings.py` with these interfaces:

```python
class Embedder(Protocol):
    dimensions: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_queries(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    def __init__(self, model_name: str, dimensions: int, cache_dir: str | None = None) -> None: ...
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_queries(self, texts: list[str]) -> list[list[float]]: ...
```

The model is instantiated lazily once per process. Blocking model initialization and inference run via `asyncio.to_thread` so API event-loop work is not blocked. Document indexing uses passage embeddings; retrieval uses query embeddings. Returned vectors are converted to plain `list[float]` values and validated against the configured dimension.

`LocalHashEmbedder` is removed from runtime wiring and evaluation. Tests use small deterministic fake embedders. No runtime path may silently fall back to hash vectors because that would recreate the failure the change is intended to fix.

### Configuration

Add exact settings and documented environment variables:

- `ATLAS_EMBEDDING_MODEL=BAAI/bge-small-en-v1.5`
- `ATLAS_EMBEDDING_DIMENSIONS=384`
- `ATLAS_EMBEDDING_CACHE_DIR` optional; defaults to FastEmbed's standard cache behavior.
- `ATLAS_QDRANT_COLLECTION=atlas_chunks_semantic_v1`
- `ATLAS_INDEX_VERSION=3`

The application must reject a configured model/dimension mismatch with an actionable `invalid_embedding_configuration` error. Secrets remain outside source control.

### Index migration and data preservation

The 1,536-dimensional hash collection is never mutated into a 384-dimensional collection. A new versioned collection is created, and the old collection remains available for rollback until an operator removes it separately.

`ensure_collection` must read existing collection vector metadata. If the selected collection exists with a different dimension or distance metric, it raises `embedding_index_mismatch` and includes the expected and observed configuration. It must not swallow collection-schema errors.

Update `scripts/reindex.py` to use `FastEmbedder` and the semantic collection. Reindexing regenerates Qdrant points from canonical SQL document records and stored source files without changing document IDs, chunk IDs, citations, audit rows, or graph data. A failed document leaves its prior SQL state and old collection intact and reports the failed document explicitly.

Render's build step downloads/caches the small ONNX model before application startup. Startup or readiness must fail clearly if the configured semantic model cannot be loaded; it must not claim semantic retrieval while using another algorithm.

## Retrieval and RFI Behavior

Existing hybrid retrieval remains: semantic dense candidates plus BM25 candidates, weighted reciprocal-rank fusion, lexical reranking, parent expansion, compression, sufficiency checking, and claim verification.

Regression coverage must demonstrate the defect is fixed rather than merely checking vector shape:

- A query using “battery runtime” retrieves evidence phrased as “battery autonomy.”
- An RFI query using a semantic paraphrase retrieves the expected previously answered RFI within the configured top results.
- Cross-project filters, document filters, index-version filters, and citation payloads still hold.
- A model or collection dimension mismatch fails before indexing or querying.
- Provider-unavailable answer generation returns valid evidence-only citations when retrieval succeeds.

Run the deterministic baseline-versus-advanced evaluation after reindexing. Acceptance requires all of the following:

- Advanced Recall@12 does not fall below its recorded `1.0` value.
- Advanced correct-document rate is greater than `0.0`.
- Advanced citation precision is greater than `0.0`.
- The new paraphrase regression cases pass without exact keyword overlap.
- Evaluation output and limitations documentation report measured results exactly, including regressions if any; no unsupported “advanced beats baseline” claim is introduced.

## Honest Product Surfaces

Preserve working backend code and synthetic workflows, but align visible claims with actual behavior:

- Rename “Mitigation simulator” and “Counterfactual mitigation simulator” to “Mitigation calculator” and describe recovery days/costs as user-supplied assumptions.
- Rename “Supply-chain simulation” to “Synthetic supply-chain demo.” Keep import and scenario functionality, but display that no live carrier, AIS, vendor, ERP, or position feed is connected.
- Describe query planning and intent selection as advisory metadata. The Copilot must not claim that it dispatched to schedule, compliance, commissioning, or procurement services when the request continued through knowledge retrieval.
- Describe ingestion as synchronous indexing. Do not present `queued`, `attempt_count`, or job status fields as proof of a background worker, retry queue, or durable orchestration.
- Remove unused JWT configuration and public documentation that implies authentication exists. Retain the explicit warning that the service must sit behind an authenticated gateway and that `project_id` is not authorization.
- Keep procurement responses labelled `demo_mock` and ensure the frontend exposes that label rather than implying live procurement state.

These changes are copy, configuration, and dead-claim cleanup only. They do not add authentication, a queue, live supply-chain integrations, a probabilistic simulator, or real service dispatch.

## Testing and Verification

Every implementation milestone follows a test-first cycle and receives its own focused verification, commit, and push to canonical `main`.

The final local gate is:

```text
python -m pytest -q
python -m compileall -q app scripts evaluation migrations
python -m evaluation.run_all
frontend: npm test
frontend: npm run lint
frontend: npm run typecheck
frontend: npm run build
```

Then run the application locally with its configured PostgreSQL and Qdrant dependencies, seed a clean project, ingest/reindex the synthetic corpus, ask a paraphrased Copilot question, run an RFI match, and exercise the renamed calculator/demo surfaces. Confirm citations reference real project documents and the health/readiness endpoints report accurately.

After local success and push, test the deployed Render API and Netlify frontend:

- API health and dependency readiness.
- Project listing and document ingestion/seed behavior.
- Paraphrased Copilot retrieval with valid citations.
- RFI matching.
- Visible demo-only/advisory/synchronous labels.
- No frontend route, asset, or API-origin regression.

A deployment failure is not hidden or worked around with hash embeddings. Diagnose it, commit a tested production fix, push it to canonical `main`, wait for redeployment, and repeat the failed smoke test.

## Commit and Push Sequence

1. `docs: define production reconciliation and retrieval design`
2. `merge: reconcile source copilot fixes into production`
3. `feat: add production-safe semantic embeddings`
4. `test: prove semantic retrieval and RFI paraphrase matching`
5. `fix: label demo and advisory product surfaces honestly`
6. `docs: publish measured retrieval and deployment results`

Tasks may combine adjacent commits only when they form one indivisible tested change. Each commit is pushed to `Prabhav200511/project-Atlas` `main` immediately after its verification passes. No push is made to `nagateja2004/project-Atlas`.

## Non-Goals

- No force-push, history rewrite, deletion of source-repository history, or push to the read-only repository.
- No commitment of the untracked submission PDF without separate approval.
- No new product areas.
- No authentication/RBAC implementation, asynchronous job queue, live supply-chain integration, graph database migration, object storage migration, or multi-tenant authorization in this scope.
- No claim that semantic retrieval is improved unless the checked-in evaluation report demonstrates it.
