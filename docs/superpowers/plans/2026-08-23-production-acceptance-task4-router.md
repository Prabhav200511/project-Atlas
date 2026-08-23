# Production Acceptance, Task 4, and Correctness-First Router Implementation Plan

> **Execution mode:** Inline in the current Codex task. Use `superpowers:test-driven-development` for every behavior change, `superpowers:systematic-debugging` for unexpected failures, and `superpowers:verification-before-completion` before each commit, push, or production claim.

**Goal:** Prove the complete deployed Atlas feature set, reconcile the reviewed Task 4 retrieval and ingestion work onto current production, fix its remaining real-data status defects, and deploy a six-provider correctness-first router with purpose-specific canary admission.

**Architecture:** One reusable synthetic acceptance manifest gates the existing deployment and every later checkpoint. Task 4 behavior is ported selectively from `codex/atlas-production-hardening` onto current production instead of merging that stale tree wholesale. A provider-neutral in-process router then replaces `GeminiGateway`; adapters perform transport only, while privacy, purpose ordering, budgets, circuits, canary admission, schema validation, and Atlas evidence verification remain centralized.

**Tech stack:** Python 3.11+, FastAPI, SQLAlchemy/Alembic, PostgreSQL, Qdrant, FastEmbed, httpx, Google GenAI SDK, pytest, Next.js 16, Vitest, Render, Netlify.

**Approved spec:** `docs/superpowers/specs/2026-08-23-production-acceptance-task4-router-design.md`

**Starting production SHA:** `696ecfc9634a659ecd1faedea31d8b74e97b9393`

## Global Constraints

- Production source is `https://github.com/Prabhav200511/project-Atlas`, branch `main`.
- Backend is `https://project-atlas-rd7v.onrender.com`; frontend is `https://project-atlas-production.netlify.app`.
- Never print, persist, or commit secrets, prompts, document text, completions, database URLs, Qdrant payload text, or raw provider bodies.
- Production mutations are restricted to an unmistakably named synthetic canary project. No customer project is selected or changed.
- The canary project remains because no project-delete endpoint exists.
- Do not change `evaluation/latest.md` or `evaluation/latest.json`.
- Do not lower retrieval, sufficiency, grounding, citation, revision, or unsupported-claim thresholds to pass a test.
- Preserve current product-truth labels and the scaling design.
- Commit locally after a coherent RED/GREEN task. Push `HEAD:main` only at a deployable checkpoint, then verify Render and Netlify report the exact SHA.
- If production verification fails, stop promotion, capture the sanitized boundary, reproduce locally, and fix through a new RED/GREEN commit.
- Missing credentials for standby providers are an expected disabled state, not a readiness failure.

---

### Task 0: Restore the Approved Enterprise-Scaling Artifact

**Files:**
- Add from existing local commit: `docs/superpowers/specs/2026-08-23-production-scaling-design.md`

- [ ] **Step 1: Create the isolated execution worktree and reuse the configured runtime**

```powershell
$AtlasRoot = "C:\Users\manik\OneDrive\Desktop\Atlas2.0"
$ExecutionTree = "$AtlasRoot\.worktrees\production-acceptance-router"
git -C $AtlasRoot fetch origin
git -C $AtlasRoot worktree add $ExecutionTree -b codex/production-acceptance-router origin/main
Set-Location $ExecutionTree
if (-not (Test-Path .venv)) {
    New-Item -ItemType Junction -Path .venv -Target "$AtlasRoot\.venv" | Out-Null
}
git status --short --branch
```

Expected: clean worktree at the implementation-plan commit, with the workspace Python environment available at `.venv`.

- [ ] **Step 2: Restore only the approved scaling document**

Commit `e50371657bdddf847d3fedc34c25d5c51a09950f` is a local docs-only scaling design that never reached production. Verify its scope, then cherry-pick it:

```powershell
git show --stat e50371657bdddf847d3fedc34c25d5c51a09950f
git show --name-only --format= e50371657bdddf847d3fedc34c25d5c51a09950f
git cherry-pick e50371657bdddf847d3fedc34c25d5c51a09950f
git diff HEAD^ --check
```

Expected: the only added path is `docs/superpowers/specs/2026-08-23-production-scaling-design.md`.

- [ ] **Step 3: Push the documentation checkpoint**

```powershell
git push origin HEAD:main
```

This commit does not alter deployed application behavior.

---

### Task 1: Build and Run the Complete Production Acceptance Baseline

**Files:**
- Create: `scripts/production_acceptance.py`
- Create: `tests/test_production_acceptance.py`
- Create: `docs/operations/production-acceptance.md`
- Modify: `.gitignore` for local raw acceptance artifacts only

**Interfaces:**
- Input: `--api-url`, `--frontend-url`, `--project-name`, `--output`, and an explicit `--allow-synthetic-mutations` flag.
- Output: sanitized JSON plus Markdown status matrix containing deployed SHA, step name, `PASS|FAIL|BLOCKED|NOT_APPLICABLE`, HTTP status, duration, and opaque created IDs.
- Safety: without `--allow-synthetic-mutations`, state-changing steps are reported `BLOCKED` and make zero mutation calls.

- [ ] **Step 1: Confirm the exact execution baseline**

```powershell
git fetch origin
git rev-parse HEAD
git rev-parse origin/main
git status --short --branch
```

Expected: the worktree is clean and contains the implementation-plan commit plus the docs-only scaling commit from Task 0. `HEAD` and `origin/main` match.

- [ ] **Step 2: Write the mutation-safety tests first**

In `tests/test_production_acceptance.py`, use `httpx.MockTransport` and assert:

```python
def test_mutating_steps_require_explicit_synthetic_permission(): ...
def test_project_selection_never_falls_back_to_an_existing_project(): ...
def test_report_redacts_content_and_raw_provider_errors(): ...
def test_failed_semantic_assertion_is_not_reported_as_pass(): ...
def test_resume_reuses_only_the_exact_canary_name(): ...
```

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_production_acceptance.py -q
```

Expected: RED because the acceptance module does not exist.

- [ ] **Step 3: Implement the result contract and guarded client**

Create dataclasses or Pydantic models for `AcceptanceRun`, `AcceptanceStep`, and opaque `AcceptanceState`. Centralize HTTP calls in a client that records only method, route template, status class, duration, and selected IDs. Require both the mutation flag and a project name matching `^Atlas Production Canary ` before POST, PATCH, or upload calls.

- [ ] **Step 4: Implement the backend manifest**

Exercise, in dependency order:

1. `/health`, `/ready`, `/openapi.json`, and production CORS.
2. Project create/list and exact canary resolution.
3. Synthetic document upload, ingestion result, list, and status.
4. Retrieval, context, query plan, Copilot, citations, RFI matches, and graph.
5. Compliance check/list/review/evaluation.
6. Schedule analysis and snapshots.
7. Commissioning procedure, records, and readiness.
8. Supply-chain seed/import, dashboard, assessments, reassessment, alerts, timeline, shipment risk, alternatives, and synthetic risk injection.
9. Executive summary and digital thread.
10. Impact-chain start/read/decision.
11. Evaluation run/read.
12. Mitigation simulate/select.
13. Benchmark create/summary.
14. Demo reset and vertical scenario within the canary project.

Build payloads from IDs created earlier in the same run. A 2xx response passes only when endpoint-specific semantic invariants pass; for example, citations require non-empty document and chunk identifiers, a selected mitigation must reference the requested simulation, and a human decision must be distinguishable from an AI suggestion.

- [ ] **Step 5: Add frontend identity and API-link probes**

The script performs read-only HTML identity checks. Full browser interaction remains a separate browser gate. Assert the frontend contains Atlas identity and excludes the unrelated climate application.

- [ ] **Step 6: Make focused and full local tests green**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_production_acceptance.py tests\test_health.py tests\test_api_errors.py -q
.\.venv\Scripts\python.exe -m pytest -q
Push-Location frontend
npm.cmd test
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run build
Pop-Location
git diff --check
```

- [ ] **Step 7: Commit and push the baseline runner**

```powershell
git add scripts/production_acceptance.py tests/test_production_acceptance.py docs/operations/production-acceptance.md .gitignore
git diff --cached --check
git commit -m "test: add complete production acceptance manifest"
git push origin HEAD:main
```

Wait until Render and Netlify identify the pushed SHA.

- [ ] **Step 8: Run the baseline against production**

```powershell
$Sha = (git rev-parse --short HEAD).Trim()
$Canary = "Atlas Production Canary 2026-08-23-$Sha"
.\.venv\Scripts\python.exe scripts\production_acceptance.py `
  --api-url https://project-atlas-rd7v.onrender.com `
  --frontend-url https://project-atlas-production.netlify.app `
  --project-name $Canary `
  --allow-synthetic-mutations `
  --output .superpowers/sdd/2026-08-23-production-acceptance-router/baseline.json
```

Expected: the runner completes every independent step even when another step fails, while dependency-blocked steps are labelled `BLOCKED` rather than producing cascaded false failures.

- [ ] **Step 9: Run the rendered browser baseline**

Using the signed-in in-app browser, exercise overview, upload, Copilot/RFI, compliance, evidence dashboard, digital thread, impact chain, commissioning, supply chain, mitigation, and evaluation on the exact canary project. Inspect console and failed requests. Test desktop and mobile width. Record only the sanitized matrix in `docs/operations/production-acceptance.md`.

- [ ] **Step 10: Commit and push the truthful baseline report**

```powershell
git add docs/operations/production-acceptance.md
git diff --cached --check
git commit -m "docs: record production feature baseline"
git push origin HEAD:main
```

Do not repair failures in this commit.

---

### Task 2: Port Semantic Retrieval and Dimension-Safe Indexing

**Files:**
- Modify: `app/config.py`
- Modify: `app/main.py`
- Modify: `app/ingestion.py`
- Modify: `scripts/reindex.py`
- Modify: `scripts/evaluate_rag.py`
- Create: `scripts/cache_embedding_model.py`
- Modify: `render.yaml`
- Port/extend tests: `tests/test_config.py`, `tests/test_ingestion.py`, `tests/test_semantic_retrieval.py`, `tests/test_hybrid_retrieval.py`, `tests/test_index_migration.py`

- [ ] **Step 1: Record the exact Task 4 source commits**

```powershell
git show --stat 1cfea55
git show --stat 3d45121
git diff origin/main...codex/atlas-production-hardening -- app/config.py app/main.py app/ingestion.py scripts/reindex.py scripts/evaluate_rag.py render.yaml
```

Use these as behavioral references. Do not checkout the files wholesale.

- [ ] **Step 2: Port the semantic RED tests**

Prove:

- Production defaults use `BAAI/bge-small-en-v1.5` and exactly 384 dimensions.
- The active runtime is `FastEmbedder`, not `LocalHashEmbedder`.
- Query and document embeddings use separate methods.
- Real paraphrases retrieve the correct construction document.
- Existing Qdrant collections fail with structured expected/actual diagnostics for size, distance, named vectors, and sparse-only layouts.
- Index version changes require explicit migration/reindex behavior.

Run the focused tests and capture the expected failures before product edits.

- [ ] **Step 3: Port the minimal semantic implementation**

Replace active hash embeddings with `app.embeddings.FastEmbedder`; configure model, dimension, and cache consistently; inspect the live collection configuration before writes; preserve hybrid dense/BM25 and parent-chunk retrieval; migrate every script caller away from the removed compatibility `embed()` method.

- [ ] **Step 4: Verify real-model behavior**

```powershell
.\.venv\Scripts\python.exe scripts\cache_embedding_model.py
.\.venv\Scripts\python.exe -m pytest tests\test_semantic_retrieval.py tests\test_hybrid_retrieval.py tests\test_index_migration.py tests\test_ingestion.py tests\test_config.py -q
```

Expected: cache output names the exact model and `384` dimensions; focused suite passes.

- [ ] **Step 5: Commit locally without pushing**

```powershell
git add app/config.py app/main.py app/ingestion.py scripts/reindex.py scripts/evaluate_rag.py scripts/cache_embedding_model.py render.yaml tests
git diff --cached --check
git commit -m "feat: restore production semantic retrieval"
```

This is not yet a deployable checkpoint because ingestion repair and index migration must land with it.

---

### Task 3: Port Stable, Serialized, and Recoverable Ingestion Repair

**Files:**
- Modify: `app/api.py`
- Modify: `app/models.py`
- Modify: `app/ingestion.py`
- Modify: `app/graph.py`
- Create: `migrations/versions/20260721_10_ingestion_attempt_order.py`
- Create: `scripts/recover_ingestion.py`
- Modify: `scripts/seed_demo.py`
- Port/extend tests: `tests/test_ingestion.py`, `tests/test_ingestion_attempt_migration.py`, `tests/test_ingestion_recovery.py`, `tests/test_seed_demo.py`

- [ ] **Step 1: Port concurrency and failure RED tests first**

Cover:

- Exact stable-ID reupload preserves Document, RFI, EvidenceLink, audit, graph-node, and citation identifiers.
- Content, document type, MIME, or ambiguous same-filename candidates reject with zero mutation.
- Canonical source replacement is staged and atomic; write and database failures restore the prior source.
- Repair versus retry, retry versus retry, repair versus forced reindex, queued handoff, and concurrent operator recovery have one owner and fenced losers.
- A stale owner cannot complete database, graph, or Qdrant writes after ownership loss.
- Reindex creates no duplicate graph edges.
- Attempt numbers are monotonic and unique, including same-second jobs.
- Fresh-upload failures contain a real persisted ingestion-job ID.
- Seed reruns treat the exact healthy conflict as success and remain restartable.

- [ ] **Step 2: Port the model and migration contract**

Add active-job, owner-token, lease, and attempt-order fields from the reviewed Task 4 implementation. The migration must upgrade old SQLite and PostgreSQL rows, repair partial application, enforce uniqueness, and support its documented downgrade. Migration-era processing rows must either receive valid recoverable ownership state or be explicitly operator-recoverable.

- [ ] **Step 3: Port claim, fence, and atomic replacement logic**

Every downstream side effect must be guarded by current ownership. Avoid a fixed lease that permits a live stale owner to overwrite the winner; the reviewed normal-runtime design uses no automatic live-worker takeover. Operator recovery requires explicit confirmation that the worker is stopped, the exact active pointer, and exactly one processing job.

- [ ] **Step 4: Make the focused matrix green**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_ingestion.py tests\test_ingestion_attempt_migration.py tests\test_ingestion_recovery.py tests\test_seed_demo.py tests\test_index_migration.py -q
.\.venv\Scripts\python.exe -m alembic heads
```

Expected: all tests pass and exactly one Alembic head is reported.

- [ ] **Step 5: Commit locally without pushing**

```powershell
git add app/api.py app/models.py app/ingestion.py app/graph.py migrations/versions/20260721_10_ingestion_attempt_order.py scripts/recover_ingestion.py scripts/seed_demo.py tests
git diff --cached --check
git commit -m "fix: make document repair stable and recoverable"
```

---

### Task 4: Fix the Remaining Real Revision-Status Defects

**Files:**
- Modify: `app/ingestion.py`
- Modify: `app/vector.py`
- Modify: `app/workflow.py`
- Modify if required: `app/context.py`
- Modify: `tests/test_ingestion.py`
- Modify: `tests/test_query_planning.py`
- Modify: `tests/test_knowledge_workflow.py`
- Modify: `tests/test_answer_generation.py`
- Modify: `tests/test_sufficiency.py`
- Create or extend: `tests/test_revision_status_integration.py`

- [ ] **Step 1: Reproduce the metadata mismatch with real Qdrant**

Ingest the actual synthetic `CO-001` fixture. Assert the normalized selection status is indexed under the same field filtered by `QueryPlan.revision_status`. Prove the existing behavior returns zero explicit-proposed results before implementing the fix.

- [ ] **Step 2: Define one normalized contract**

Use `revision_status` as the selection field for specifications, submittals, RFIs, and change orders. Preserve `rfi_status` only as domain metadata where needed. During the compatibility window, retrieval accepts legacy payloads without letting unrequested proposed material leak into current-answer workflows.

- [ ] **Step 3: Reproduce query-intent edge cases**

RED cases include:

```text
Can you show proposed recovery measures?
Show records issued for review.
Tell me whether the approved submittal meets the rating.
Is CO-001 approved?
Do not use proposed documents.
The RFI body says Status: Proposed. What cable size is required?
Proposed answer: use the approved specification. What is the required rating?
```

Only the first two are positive non-current selection filters. The approval questions, negation, and document-body mentions must not become a selection filter.

- [ ] **Step 4: Implement current-utterance status selection**

Parse only the current user query, normalize multiword statuses, distinguish imperative/selection language from yes/no status questions, and apply the same sanitized result to local and provider plans. Do not infer a filter from retrieved content, multiline RFI bodies, or earlier assistant output.

- [ ] **Step 5: Prove full workflow behavior**

Run actual ingestion, retrieval, context sufficiency, generated-answer, and provider-outage deterministic fallback cases. Requested proposed evidence must remain clearly labelled; unrequested proposed or superseded evidence must not be stated as current fact. Do not mock `_retrieve_batches` for the integration proof.

- [ ] **Step 6: Commit locally**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_revision_status_integration.py tests\test_query_planning.py tests\test_knowledge_workflow.py tests\test_answer_generation.py tests\test_sufficiency.py -q
git add app/ingestion.py app/vector.py app/workflow.py app/context.py tests
git diff --cached --check
git commit -m "fix: align revision status selection end to end"
```

---

### Task 5: Gate, Deploy, and Re-Test the Task 4 Checkpoint

**Files:**
- Modify: `docs/operations/production-acceptance.md`
- Modify if required by deployment: `docs/operations/production-deployment.md`

- [ ] **Step 1: Run the exact-tree Task 4 gate**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall app scripts
.\.venv\Scripts\python.exe scripts\cache_embedding_model.py
.\.venv\Scripts\python.exe scripts\evaluate_rag.py --output-dir .superpowers/sdd/2026-08-23-production-acceptance-router/task4-eval
Push-Location frontend
npm.cmd test
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run build
Pop-Location
git diff --check
git status --short
```

Also upgrade a database from revision `20260721_09`, downgrade the Task 4 migration where promised, and re-upgrade. Verify Recall@12, correct-document, citation precision, and unsupported-claim gates from the approved design.

- [ ] **Step 2: Review the complete production diff**

Compare `696ecfc..HEAD` for loss of GPT-OSS-120B configuration, current deployment URLs, scaling documentation, product-truth labels, or unrelated generated artifacts. Run a focused security-diff review because this checkpoint changes file replacement, concurrency ownership, and external vector writes.

- [ ] **Step 3: Push the deployable Task 4 checkpoint**

```powershell
git push origin HEAD:main
```

Wait for Render and Netlify to report the exact SHA. Verify migration completion, `/ready`, embedding model/dimension diagnostics, and absence of startup reindex loops.

- [ ] **Step 4: Repeat the complete production manifest and browser matrix**

Reuse the dedicated synthetic project only when its exact name matches; otherwise create the new SHA-labelled canary. Compare every result to the baseline. All baseline passes must remain passes, known Task 4 failures must close, and no unexplained new failures are allowed.

- [ ] **Step 5: Commit and push the sanitized Task 4 report**

```powershell
git add docs/operations/production-acceptance.md docs/operations/production-deployment.md
git diff --cached --check
git commit -m "docs: verify Task 4 in production"
git push origin HEAD:main
```

---

### Task 6: Introduce Provider-Neutral Contracts, Policy, and Routing

**Files:**
- Create: `app/model_router/__init__.py`
- Create: `app/model_router/contracts.py`
- Create: `app/model_router/policy.py`
- Create: `app/model_router/router.py`
- Create: `app/model_router/audit.py`
- Modify: `app/config.py`
- Modify: `app/models.py`
- Create: `migrations/versions/20260823_11_model_invocations.py`
- Create: `tests/test_model_router.py`
- Create: `tests/test_model_privacy.py`
- Create: `tests/test_model_invocation_migration.py`

- [ ] **Step 1: Write contract and zero-call privacy RED tests**

Model requests carry request ID, optional project ID, purpose, content, structured-output schema requirement, classification, total deadline, and maximum attempts. Tests prove deterministic purpose ordering, deadline propagation, no provider call for prohibited classifications, no fallback for invalid credentials/input, and fallback only for normalized retryable failures.

- [ ] **Step 2: Implement contracts and eligibility**

Define `ModelProvider` as an async protocol. Define normalized result and error categories. Compute candidates from enabled configuration, credentials, privacy permission, capabilities, daily budget, canary admission, circuit state, and remaining deadline.

- [ ] **Step 3: Implement purpose-specific ordering**

Defaults:

```text
planning:     groq, gemini, mistral, nvidia, cloudflare, openrouter
answering:    gemini, mistral, groq, nvidia, cloudflare, openrouter
verification: gemini, mistral, groq, nvidia, cloudflare, openrouter
```

Configuration may override model IDs and enabled state but not bypass privacy or canary admission.

- [ ] **Step 4: Implement circuit, budget, and sanitized audit metadata**

Use bounded process-local circuits for the current single Render instance. Store one `model_invocations` row per attempt with metadata only; prompts, evidence, completions, raw errors, and keys are structurally absent from the table. Migration tests inspect the real schema.

- [ ] **Step 5: Make focused tests green and commit locally**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_model_router.py tests\test_model_privacy.py tests\test_model_invocation_migration.py tests\test_config.py -q
git add app/model_router app/config.py app/models.py migrations/versions/20260823_11_model_invocations.py tests
git diff --cached --check
git commit -m "feat: add correctness-first model routing policy"
```

---

### Task 7: Add and Contract-Test Six Provider Adapters

**Files:**
- Create: `app/model_router/providers/base.py`
- Create: `app/model_router/providers/groq.py`
- Create: `app/model_router/providers/gemini.py`
- Create: `app/model_router/providers/mistral.py`
- Create: `app/model_router/providers/nvidia.py`
- Create: `app/model_router/providers/cloudflare.py`
- Create: `app/model_router/providers/openrouter.py`
- Create: `app/model_router/providers/__init__.py`
- Create: `tests/model_providers/conftest.py`
- Create: `tests/model_providers/test_contract.py`
- Create focused transport tests under: `tests/model_providers/`
- Modify: `.env.example`

- [ ] **Step 1: Write one parametrized adapter contract**

For every adapter, mock the exact outbound transport and assert authentication placement, timeout use, structured-output request, response parsing, usage extraction, request-ID capture, sanitization, and normalized `429`, `5xx`, auth, timeout, malformed JSON, and schema-invalid behavior.

- [ ] **Step 2: Implement Gemini and Groq first**

Gemini defaults to `gemini-3.5-flash`. Groq defaults to `openai/gpt-oss-120b` and requests strict JSON schema when the purpose supplies a schema. Preserve the shared prompt-injection boundary without duplicating it inconsistently between adapters.

- [ ] **Step 3: Implement Mistral and NVIDIA NIM**

Keep base URL and model configurable. Require the same schema contract; a provider/model that cannot satisfy it remains ineligible for the relevant purpose.

- [ ] **Step 4: Implement Cloudflare and OpenRouter**

Cloudflare is a standby because JSON mode alone does not guarantee the Atlas schema. OpenRouter must enforce zero-data-retention/provider restrictions and required parameters; it is emergency-only.

- [ ] **Step 5: Prove missing keys disable adapters safely**

Startup with only Gemini and Groq keys must succeed. Startup with no provider keys must preserve deterministic safe fallback. Configuration diagnostics list provider names and enabled states only, never key fragments.

- [ ] **Step 6: Commit locally**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\model_providers tests\test_model_router.py tests\test_model_privacy.py -q
git add app/model_router/providers tests/model_providers .env.example
git diff --cached --check
git commit -m "feat: add six model provider adapters"
```

---

### Task 8: Integrate the Router and Add Purpose-Specific Canaries

**Files:**
- Modify: `app/llm.py`
- Modify: `app/main.py`
- Modify: `app/workflow.py`
- Modify: `app/compliance.py`
- Modify: `app/schedule.py`
- Create: `app/model_router/canary.py`
- Create: `scripts/check_model_providers.py`
- Create: `tests/test_model_canary.py`
- Modify: `tests/test_llm_gateway.py`
- Modify: `tests/test_query_planning.py`
- Modify: `tests/test_answer_generation.py`

- [ ] **Step 1: Write router-disabled parity RED tests**

With `ATLAS_MODEL_ROUTER_ENABLED=0`, existing planner, compliance, schedule, and answer behavior must match the Task 4 checkpoint. With it enabled for synthetic requests, the service supplies explicit purpose and classification and preserves all evidence verification downstream.

- [ ] **Step 2: Replace `GeminiGateway` with a compatibility facade**

Call sites receive a provider-neutral gateway. Keep a temporary import-compatible facade only if needed to avoid a big-bang call-site migration, but remove Groq-first hidden selection from `app/llm.py`. Purpose is explicit at every call.

- [ ] **Step 3: Implement purpose-specific canaries**

The canary checks connectivity, schema adherence, planner normalization, citation-shaped answer generation, Atlas grounding, explicit proposed/multiword selection, insufficient evidence, and privacy zero-call behavior. Admission is per provider/model/purpose with a timestamp and failure category.

- [ ] **Step 4: Add the operator canary script**

`scripts/check_model_providers.py` prints only provider, model, purpose, admission state, latency, and normalized result. It never prints prompts, output, keys, raw provider errors, or evidence.

- [ ] **Step 5: Run dark-launch gates**

```powershell
$env:ATLAS_MODEL_ROUTER_ENABLED = "0"
.\.venv\Scripts\python.exe -m pytest -q
Remove-Item Env:ATLAS_MODEL_ROUTER_ENABLED
Push-Location frontend
npm.cmd test
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run build
Pop-Location
git diff --check
```

- [ ] **Step 6: Commit, review, and push the dark launch**

```powershell
git add app tests scripts .env.example migrations
git diff --cached --check
git commit -m "feat: dark launch the Atlas model router"
git push origin HEAD:main
```

Verify the exact deployed SHA and repeat the acceptance manifest with routing disabled. Any difference from the Task 4 checkpoint blocks enablement.

---

### Task 9: Admit Live Providers, Enable Synthetic Routing, and Finish Production Verification

**Files:**
- Modify: `render.yaml` for non-secret default flags only
- Modify: `docs/operations/production-deployment.md`
- Modify: `docs/operations/production-acceptance.md`
- Modify: `README.md`

- [ ] **Step 1: Update non-secret production configuration**

Set Gemini to `gemini-3.5-flash`, keep Groq at `openai/gpt-oss-120b`, and enable the router only for synthetic-classified requests. Do not configure absent Mistral, NVIDIA, Cloudflare, or OpenRouter secrets. Read back variable names and masked status only.

- [ ] **Step 2: Run live Gemini and Groq canaries**

Expected initial policy:

- Groq is admitted for planning if its schema canary passes.
- Gemini is admitted for answering and verification only if the complete Atlas grounding canary passes.
- Groq answering remains blocked if generated claims fail grounding; a provider HTTP 200 does not override this.
- Missing-key providers report `disabled_missing_credentials`.

If Gemini or Groq fails, diagnose the exact boundary locally. Do not reorder merely to make a failing provider appear healthy; update admission from evidence.

- [ ] **Step 3: Repeat full local and production gates**

Run the exact-tree backend, frontend, migration, semantic cache, evaluation, provider contract, privacy, fault-injection, and acceptance suites. In production, repeat every backend and rendered-browser manifest step on the synthetic project. Require zero unexplained `500`s, console errors, prohibited outbound calls, accepted unsupported claims, or citation-integrity failures.

- [ ] **Step 4: Record the observed active order**

Document static preference, eligible providers by purpose, canary timestamps, disabled reasons, fallback observations, and free-tier limitations. Do not publish account quotas or secret-derived details.

- [ ] **Step 5: Commit and push final verified status**

```powershell
git add render.yaml README.md docs/operations/production-deployment.md docs/operations/production-acceptance.md
git diff --cached --check
git commit -m "docs: verify correctness-first production routing"
git push origin HEAD:main
git fetch origin
git rev-parse HEAD
git rev-parse origin/main
git status --short
```

Expected: local and remote SHA match, worktree is clean, Render and Netlify report the same SHA, and all production gates remain green.

## Rollback Points

| Checkpoint | Rollback action |
|---|---|
| Acceptance runner/report only | Revert the documentation or runner commit; deployed application behavior is unchanged. |
| Task 4 reconciliation | Redeploy SHA `696ecfc9634a659ecd1faedea31d8b74e97b9393` only after confirming the Task 4 database migration is backward-compatible for that rollback. |
| Router dark launch | Set `ATLAS_MODEL_ROUTER_ENABLED=0`; if necessary redeploy the verified Task 4 SHA. |
| Synthetic router enablement | Disable synthetic router traffic first, then disable the failing provider/model purpose admission, then redeploy the dark-launch SHA if application rollback is required. |

Every rollback is followed by `/health`, `/ready`, CORS, frontend identity, synthetic ingestion, grounded Copilot, and browser checks.

## Completion Evidence

Completion requires:

- Exact local test totals and durations.
- Frontend test, lint, typecheck, and build results.
- Alembic head and migration lifecycle evidence.
- Semantic model and dimension readback.
- Evaluation metrics against fixed thresholds.
- Provider-purpose canary states and sanitized failure categories.
- Complete production endpoint and browser matrices.
- Render, Netlify, local, and `origin/main` SHA convergence.
- A clean worktree and `git diff --check`.
