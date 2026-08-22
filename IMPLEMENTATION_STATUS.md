## Completed
- Foundation: FastAPI, configuration, PostgreSQL/Qdrant clients, data models, health check, workflow skeleton, local dependencies, and focused tests.
- Fully synthetic, project-scoped EPC demo corpus with compliance, RFI similarity, schedule-risk, commissioning, and context test cases.
- Project-scoped document ingestion: upload validation, PDF/CSV extraction, conditional OCR fallback, metadata/chunking, local deterministic embeddings, Qdrant indexing, hybrid retrieval, citations, duplicate protection, and failure tracking.
- Knowledge Copilot and RFI Intelligence LangGraph workflows, plus NetworkX entity extraction and project-scoped graph JSON export.
- Specification and Quality Compliance Agent with deterministic normalized comparisons, reviewer audit trail, APIs, and ground-truth metrics.
- Schedule Intelligence Agent with deterministic dependency validation, CPM/float calculations, scenario delay propagation, risk classification, evidence, API, and ground-truth tests.
- Commissioning Copilot with procedure steps, observation assessment, coverage, structured records, and non-conformance creation.
- Procurement Risk Agent demo contracts, dashboard-compatible mock responses, and explicit live-integration roadmap states.
- Next.js EPC dashboard with project selection, document ingestion, all agent views, typed FastAPI client, loading/empty/error states, and desktop presentation layout.
- Synthetic end-to-end evaluation verified 27/27 document ingestions, compliance TP/FP/FN of 6/0/0 (precision/recall 1.0), both RFI pairs at rank 1 (Recall@5 1.0), 17/17 citation checks, 35-day schedule lead time, and 100% commissioning coverage.
- Hackathon delivery pack verified: README/run scripts, API seeding script, architecture diagram, API reference, demo script, nine-slide content, recording checklist, backup visual manifest, and built-versus-roadmap documentation.
- Gemini LLM gateway with centralized model/key handling, request-side prompt-injection rejection, and untrusted-evidence instructions for all model calls.
- Project-scoped query preprocessing with one structured Gemini plan call (and deterministic local fallback), standalone follow-up rewriting, intent detection, filter sanitization, and routing to existing services.
- QueryPlan-scoped hybrid retrieval: top-20 dense and BM25 candidates, metadata filters, chunk deduplication, reciprocal-rank fusion, and top-12 cited results.
- Deterministic post-retrieval processing with local cross-encoder reranking, diverse top-5–8 selection, selective parent expansion, overlap removal, extractive compression, approved-revision preference/conflict reporting, and configured context-token limits.
- ContextBundle-only answer generation with typed claims/status, inline chunk citation mapping, conflict/insufficient-evidence reporting, and deterministic rejection of invalid citations or unsupported factual values.
- Versioned contextual child indexing with original quote text, persisted parent-section points, table-header preservation, legacy payload compatibility, and an explicit project/document-scoped reindex command.
- Multi-part QueryPlan fan-out (maximum three), cross-query RRF deduplication, shared reranking/compression, and a deterministic evidence-sufficiency gate with one corrective project-only retry.
- Deterministically verified grounded answers with claim-level support status, exact citation spans, conflict metadata, unit/identifier validation, unsupported-claim removal, and at most one batched semantic check for uncertain claims.
- Unified Knowledge Copilot LangGraph with explicit planning, routing, retrieval, fusion, reranking, expansion, compression, sufficiency/retry, generation, verification, and finalization nodes plus content-safe execution metrics.
- Reproducible baseline-versus-advanced RAG harness with development-only parameter tuning, held-out test evaluation, exact citation/claim scoring, latency/token/retry metrics, and JSON/Markdown reports; the current held-out result makes no improvement claim.

## In progress
- Deployment remediation and verification: the configured Netlify target served an unrelated climate-awareness site on 2026-08-22, while the configured Render target accepted TCP 443 but returned no HTTP response from `/health` or `/ready` within the probe limits.
- Semantic-runtime evaluation is intentionally paused/deferred. It has not been merged into this canonical branch, and no production seed, re-upload, Copilot, or RFI proof has been performed.

## Remaining
- Remediate the Netlify and Render deployments, then repeat dated health/readiness, dashboard, and API-origin verification.
- Resume the deferred semantic-runtime evaluation only when authorized; regenerate final semantic and production measurements only after its local and deployed smoke tests complete.
- Provision and apply database migrations for production.

## Known issues
- Production migrations are not provisioned; `.env.example` enables local schema creation for the Compose database.
- Live knowledge responses and optional narrative enrichment require `ATLAS_GEMINI_API_KEY`; scanned-PDF fallback also requires the system Tesseract binary.
- `npm audit --omit=dev` reports a moderate Next bundled-PostCSS advisory with no compatible npm upgrade path reported by the registry.
- Authentic dashboard screenshots and a recording still require a locally configured Gemini key and browser capture; static, clearly labelled measured backup visuals and a capture manifest are included.
- Prompt-injection detection is a deterministic baseline; production deployment needs policy tuning, adversarial evaluation, and monitoring.
- Local verification is passing (backend 101 passed with 3 warnings; frontend 9 passed plus lint/typecheck/build), but this is configuration/build evidence only and does not establish a working live deployment.
- `evaluation/latest.json` and `evaluation/latest.md` retain their current checked-in pre-semantic values; final semantic and production measurements are deferred.
