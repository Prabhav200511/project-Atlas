# Production Acceptance, Task 4 Reconciliation, and Correctness-First Router Design

**Date:** 2026-08-23

**Status:** Approved for implementation planning

**Repository:** `Prabhav200511/project-Atlas`

**Production branch:** `main`

**Supersedes for routing order:** `2026-08-23-deployment-first-multi-provider-routing-design.md`

## Summary

Project Atlas will establish a measured production baseline, reconcile the proven Task 4 semantic-retrieval and ingestion-repair work onto the current production tree, and only then introduce a provider-neutral, correctness-first model router. Every promotion is gated by the same local and production acceptance manifest. Production tests use a dedicated synthetic project and may create state, but they do not use or mutate customer data.

Routing is purpose-specific. Planning favors fast, schema-reliable GPT-OSS-120B through Groq. Evidence-sensitive answering and verification favor an independent model family that passes Atlas grounding canaries. Static preference is only the first filter: a provider is eligible only when credentials, privacy policy, capability probes, quota, circuit state, and the current Atlas canary all permit it.

## Goals

- Test every deployed backend and frontend feature and publish a sanitized PASS, FAIL, or BLOCKED result.
- Preserve a reproducible production baseline before changing Task 4 or router behavior.
- Reconcile Task 4 without discarding newer deployment, documentation, model, or product-truth changes.
- Fix the two remaining Task 4 status-selection defects with real ingestion and full-workflow regressions.
- Restore semantic retrieval and stable, idempotent document repair in production.
- Replace the provider-specific gateway with six isolated adapters and one policy router.
- Order providers by evidence quality and schema reliability first, availability second, and latency or cost third.
- Admit providers using live Atlas canaries rather than provider marketing claims.
- Preserve deterministic insufficient-evidence and evidence-only fallback behavior.

## Non-Goals

- Claiming enterprise throughput from free provider quotas.
- Sending confidential project data to a free-tier endpoint by default.
- Weakening citation, grounding, revision-status, or evidence-sufficiency checks to make a model appear successful.
- Using fallback to evade a provider's rate limits or terms.
- Deleting the synthetic production project; the current API has no project-delete endpoint.
- Combining the complete Task 4 reconciliation and router enablement in one deployment.
- Treating an HTTP 200 from a provider as an accepted Atlas answer.

## Current Production Baseline

- Backend: `https://project-atlas-rd7v.onrender.com`
- Frontend: `https://project-atlas-production.netlify.app`
- Production source: `Prabhav200511/project-Atlas`, branch `main`
- Current model call: Groq API with `openai/gpt-oss-120b`
- Current second provider: Google Gemini, configured with an obsolete `gemini-2.0-flash` default
- Configured provider credentials: Groq and Gemini only
- Current gateway name and behavior are coupled: `GeminiGateway` attempts Groq whenever a Groq key exists and therefore does not perform policy-based failover.
- GPT-OSS-120B provider calls succeed, but the deployed Atlas workflow has rejected generated claims as unsupported. It is not admitted as the primary evidence-answering model until it passes the grounding canary.

## Delivery Sequence

The implementation is divided into four independently deployable checkpoints.

1. **Production acceptance baseline:** exercise the existing deployment without changing application behavior.
2. **Task 4 reconciliation:** replay the Task 4 behavior onto current `main`, fix the remaining status defects, and deploy it without the new router.
3. **Router dark launch:** deploy provider-neutral contracts, adapters, policy, audit metadata, and canaries with routing disabled.
4. **Synthetic canary enablement:** enable only eligible configured providers for the dedicated synthetic project, then repeat the complete acceptance manifest.

Each checkpoint has its own commit, local gate, push, deployment observation, production gate, and rollback point.

## Production Acceptance Manifest

### Test Data Boundary

The acceptance runner creates one project named with the deployed short SHA and UTC date. It uploads only repository-owned synthetic fixtures and records project, document, job, RFI, evaluation, and decision identifiers in a sanitized report. Because the API has no project deletion endpoint, the project remains in production and is clearly labelled as a synthetic canary.

The runner must not print document text, prompts, completions, secrets, database URLs, or provider error bodies. Re-running the same deployed SHA should reuse or safely re-upload within that synthetic project where endpoint semantics allow it.

### Backend Coverage

The manifest covers:

- Liveness, readiness, OpenAPI, and CORS.
- Project creation and listing.
- Document upload, ingestion completion, listing, status, and retry or repair paths.
- Retrieval, context assembly, query planning, Copilot, citations, RFI matching, and graph reads.
- Compliance checks, reviews, lists, and evaluation.
- Schedule analysis and snapshots.
- Commissioning procedure, records, and readiness.
- Supply-chain demo seed/import, dashboard, assessments, reassessment, alerts, timeline, shipments, risk, alternatives, and injection.
- Executive summary and digital thread.
- Impact-chain start, read, and decision recording.
- Evaluation run and result reads.
- Mitigation simulation and selection.
- Benchmark creation and summary.
- Demo reset and vertical-scenario endpoints when they operate only on the dedicated synthetic project.

Every endpoint receives one of:

- `PASS`: response and semantic assertions pass.
- `FAIL`: the deployed feature returned an incorrect response or invariant violation.
- `BLOCKED`: a required external credential, quota, or service state is unavailable; the exact boundary is recorded.
- `NOT_APPLICABLE`: destructive or customer-data behavior is outside the approved synthetic boundary.

### Frontend Coverage

Browser acceptance covers application identity, API connectivity, project selection, overview, upload, evidence dashboard, Copilot/RFI, compliance, digital thread, impact chain, commissioning, supply chain, mitigation, evaluation, and truthful demo/advisory labels. It also checks console errors, failed network requests, and mobile-width rendering for the primary flows.

### Baseline Rule

The first run records failures but does not fix them. A defect is fixed only after it is reproduced locally with the smallest meaningful failing test. The affected workflow, full local suites, and the production manifest must then pass before the repair is promoted.

## Task 4 Reconciliation

Task 4 is replayed onto current production rather than merging its branch wholesale. This preserves newer deployment, GPT-OSS-120B, router-design, scaling, and product-truth changes while retaining proven Task 4 behavior.

### Required Task 4 Behaviors

- Real semantic embeddings with dimension-safe Qdrant collection validation.
- Explicit diagnostics for unnamed, named, sparse-only, dimension, and distance mismatches.
- Stable document identifiers across safe missing-source repair and exact reindex.
- Immutable content hash, document type, and MIME identity during stable-ID repair.
- Serialized repair and retry ownership with fenced completion.
- Monotonic ingestion attempts and recoverable migration semantics.
- Atomic canonical-file replacement and failure recovery.
- Idempotent graph and vector writes.
- Seed restartability and healthy-conflict handling.
- Evidence generation that excludes unrequested proposed or superseded supplemental material.
- Explicit requested non-current revision status that remains clearly labelled through retrieval, generation, verification, and deterministic fallback.
- Operator recovery that requires an exact document pointer and one unambiguous processing job.

### Remaining Status Defects

Two known defects are acceptance blockers:

1. Ingested `Status: Proposed` construction records currently expose the value as `rfi_status`, while explicit retrieval plans filter Qdrant by `revision_status`. The ingestion and retrieval metadata contract must use one normalized selection field, with compatibility handling for existing indexed payloads.
2. The local status-intent heuristic mistakes document-body mentions, negated questions, and approval questions for selection filters, while missing natural selections such as “Can you show proposed…”. Status selection must be derived only from the current query utterance and must distinguish selection from asking whether something is approved.

Regressions must use actual ingestion and retrieval, plus complete Copilot workflows with provider success and provider outage. Mocking `_retrieve_batches` is insufficient for these defects.

## Provider Architecture

```text
Atlas workflow
  -> ModelRequest(purpose, classification, schema, deadline)
  -> Privacy policy
  -> Credential + capability + budget eligibility
  -> Canary admission + circuit state
  -> Purpose-specific ordered candidates
  -> Provider adapter
  -> Schema validation
  -> Atlas grounding, evidence, revision, and citation verification
  -> accepted answer or deterministic safe fallback
```

Provider adapters implement authentication, request formatting, provider response parsing, usage extraction, and normalized errors. They do not decide privacy, fallback, evidence acceptance, or provider order.

### Providers and Initial Models

| Provider | Initial model or policy | Role |
|---|---|---|
| Google Gemini | `gemini-3.5-flash` | Primary evidence answering and verification after canary admission |
| Groq | `openai/gpt-oss-120b` | Primary planning; fast structured fallback for answering after grounding admission |
| Mistral AI | `mistral-small-2603` | Independent structured-output answering standby |
| NVIDIA NIM | Configured canary-approved instruction model | High-capacity standby; no hard-coded permanent catalog choice |
| Cloudflare Workers AI | Configured canary-approved instruction model | Availability standby; schema validation is mandatory |
| OpenRouter | Configured structured-output model with zero-data-retention and required-parameter routing | Emergency standby only |

Model IDs remain configuration because provider catalogs and free tiers change. Missing credentials disable an adapter without preventing Atlas startup.

### Purpose-Specific Preference

Planning and classification:

1. Groq GPT-OSS-120B
2. Gemini 3.5 Flash
3. Mistral Small 4
4. NVIDIA NIM
5. Cloudflare Workers AI
6. OpenRouter ZDR

Evidence answering and verification:

1. Gemini 3.5 Flash
2. Mistral Small 4
3. Groq GPT-OSS-120B
4. NVIDIA NIM
5. Cloudflare Workers AI
6. OpenRouter ZDR

The effective candidate list contains only adapters that pass all eligibility gates. With the current secrets, the initial active candidate set is Gemini and Groq; the other adapters remain disabled until credentials are supplied and their live canaries pass.

### Eligibility and Fallback

A provider is attempted only when:

- It is enabled and has valid configuration.
- The request classification is permitted.
- It supports the requested structured-output and context capabilities.
- Its daily request and token budgets are not exhausted.
- Its circuit is closed or admits a bounded half-open probe.
- Its latest model-capability and Atlas workflow canaries pass.
- The remaining request deadline permits the attempt.

Fallback is allowed for connection failures, timeouts, rate limits, retryable provider `5xx`, and explicitly classified malformed provider output. It is not allowed for privacy rejection, invalid credentials, invalid application input, or evidence rejection. Evidence rejection returns Atlas's safe answer state; it does not shop the same evidence across every free provider in an attempt to obtain a passing claim.

### Canary Admission

Each provider/model pair must pass:

- Connectivity and authentication without exposing secrets.
- Required JSON schema adherence.
- Query-plan normalization.
- Citation-bearing answer shape.
- Atlas grounding and unsupported-claim rejection.
- Explicit proposed and multiword revision-selection behavior.
- Insufficient-evidence behavior.
- Timeout, rate-limit, retryable error, malformed-output, and circuit recovery behavior.
- Zero outbound calls for prohibited classifications.

A canary failure removes the provider/model pair from the affected purpose, not necessarily every purpose. For example, GPT-OSS-120B may remain eligible for planning while blocked from answering.

## Privacy and Audit

Every model request has an explicit `public`, `synthetic`, or `confidential` classification. Free-tier adapters default to public and synthetic only. Confidential routing requires an explicit per-provider deployment approval backed by current retention and training terms.

Audit records contain request ID, project ID, purpose, classification, provider, model, fallback position, result category, status class, latency, token counts when available, circuit transition, and timestamp. Prompts, evidence text, completions, secret values, and raw provider bodies are excluded.

## Observability

Metrics are segmented by provider, model, and purpose:

- Attempt, success, failure, schema-invalid, and evidence-rejected counts.
- Fallback and safe-fallback rates.
- Latency percentiles.
- Rate-limit and budget-exhaustion counts.
- Circuit state.
- Canary admission state and last successful time.
- Accepted unsupported claims, which must remain zero.

The acceptance report records the deployed SHA and status matrix without sensitive content. Provider health does not make `/ready` fail; database and Qdrant remain mandatory readiness dependencies.

## Deployment and Rollback

### Checkpoint 1: Baseline

- Run the manifest against the current deployed SHA.
- Commit the runner and sanitized baseline report.
- Do not change behavior.

### Checkpoint 2: Task 4

- Reconcile Task 4 using RED/GREEN tests.
- Run focused, full backend, frontend, migration, semantic-model, and real evaluation gates.
- Push one reviewed checkpoint and wait for Render and Netlify to deploy that SHA.
- Repeat the production manifest.
- Roll back by redeploying the recorded baseline SHA if any critical workflow regresses.

### Checkpoint 3: Router Dark Launch

- Deploy router contracts and adapters with `ATLAS_MODEL_ROUTER_ENABLED=0`.
- Prove behavior matches the Task 4 baseline.
- Roll back with the feature flag first, then application SHA if required.

### Checkpoint 4: Synthetic Canary

- Enable routing only for the dedicated synthetic project or synthetic classification.
- Admit Gemini and Groq only after their live purpose-specific canaries pass.
- Keep Mistral, NVIDIA, Cloudflare, and OpenRouter disabled until credentials and canaries are available.
- Promote wider public/synthetic traffic only after a bounded observation window and a clean acceptance rerun.

## Acceptance Gates

- Complete backend and frontend manifest with no unexplained failures.
- Backend full suite, frontend tests, lint, typecheck, and production build pass.
- Alembic upgrades from the supported pre-Task-4 revision, downgrade where promised, and re-upgrade pass on real SQLite; production PostgreSQL migration is verified before promotion.
- Semantic embedding cache warmup reports the configured model and exact dimension.
- Recall@12 at least `0.90`.
- Correct-document rate at least `0.80` for the expanded real-data corpus; the existing small synthetic diagnostic retains its historical `0.50` floor until the corpus is expanded.
- Citation precision at least `0.95`.
- Accepted unsupported claims exactly `0`.
- Explicit revision-selection integration cases pass with real indexed metadata.
- Privacy-policy enforcement produces zero prohibited outbound calls.
- Router-disabled behavior matches the Task 4 deployment.
- Each enabled provider passes its purpose-specific canary.
- Render and Netlify report the exact pushed SHA and production probes remain green.

## Risks and Mitigations

- **Task 4 branch drift:** replay behavior and tests onto current production instead of merging the old tree wholesale.
- **Provider quality changes:** purpose-specific canaries control admission and can remove one model without a deploy.
- **Free-tier volatility:** model IDs, budgets, and enabled state are configuration.
- **Fallback hides correctness defects:** provider success and Atlas evidence acceptance remain separate metrics; evidence rejection never counts as provider success.
- **Production state accumulates:** canary projects use explicit synthetic naming and deployed SHA identifiers.
- **Repair concurrency loses data:** ownership fencing, atomic file replacement, idempotent downstream writes, and failure-injection tests are required.
- **Status filters omit real records:** one normalized indexed field and compatibility tests cover legacy payloads.
- **Confidential data leaks:** privacy eligibility executes before any adapter call and is proven with call counters.

## Source References

- Provider inventory: https://github.com/mnfst/awesome-free-llm-apis
- Groq GPT-OSS-120B: https://console.groq.com/docs/model/openai/gpt-oss-120b
- Groq structured outputs: https://console.groq.com/docs/structured-outputs
- Groq rate limits: https://console.groq.com/docs/rate-limits
- Gemini 3.5 Flash: https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash
- Gemini structured outputs: https://ai.google.dev/gemini-api/docs/generate-content/structured-output
- Gemini pricing and tier data use: https://ai.google.dev/gemini-api/docs/pricing
- Mistral Small 4: https://docs.mistral.ai/models/mistral-small-4-0-26-03
- Mistral structured outputs: https://docs.mistral.ai/studio/conversations/structured-output
- Cloudflare Workers AI pricing: https://developers.cloudflare.com/workers-ai/platform/pricing/
- Cloudflare JSON mode: https://developers.cloudflare.com/workers-ai/features/json-mode/
- OpenRouter structured outputs: https://openrouter.ai/docs/guides/features/structured-outputs
- OpenRouter zero-data retention: https://openrouter.ai/docs/guides/features/zdr
