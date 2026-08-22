# Architecture

The canonical, syntax-validated diagram is [ARCHITECTURE.mermaid](ARCHITECTURE.mermaid). It was rendered with Mermaid CLI after the final architecture update.

## Implemented flow

1. The Next.js dashboard calls project-scoped FastAPI routes through one typed client.
2. FastAPI validates `project_id`; LangGraph classifies the query and may recommend an existing service endpoint, but does not dispatch it. Copilot continues through knowledge retrieval. Authentication and RBAC are not implemented.
3. Original uploads are stored on the project-scoped local filesystem. The request parses and indexes them synchronously; no background worker or retry queue is running. Structure-aware parsing creates persisted parent sections plus child chunks containing separate `original_text` and `contextual_text`.
4. Contextual text drives local deterministic dense embeddings and local BM25 ranking. Qdrant stores vectors and chunk payloads; PostgreSQL stores document metadata, workflow records, decisions, and audit events.
5. Retrieval uses project filters, dense and BM25 candidates, weighted RRF, deduplication, reranking, parent expansion, compression, revision checks, and a deterministic evidence gate with at most one corrective retry.
6. Generation receives selected evidence only. Claim identifiers, citation identifiers, exact values, and supporting spans are verified before the response is returned.
7. Deterministic compliance, CPM schedule, synthetic supply-chain, and commissioning engines populate the Equipment Digital Thread and the Impact Chain mitigation workflow.
8. NetworkX provides an in-memory/project-JSON prototype graph. The evaluation harness writes machine-readable and Markdown reports; structured logs and content-safe workflow traces expose stage latency, candidate IDs, tokens, retries, and status.

## Roadmap boundary

Dashed ROADMAP nodes in the diagram are not active: authentication/RBAC, live AIS/geospatial feeds, computer vision, ERP/QMS/P6 integrations, and production graph storage. Local document storage, local deterministic embeddings, and the NetworkX prototype must not be presented as their production equivalents.

## Storage ownership

| Store | Implemented responsibility |
| --- | --- |
| Local filesystem | Original uploaded documents, scoped under project/document paths |
| PostgreSQL | Projects, document/job metadata, findings, reviews, audit events, schedules, shipments, commissioning records, NCRs, mitigations, evidence links |
| Qdrant | Project-filtered dense vectors and contextual chunk payloads used by dense and BM25 retrieval |
| NetworkX/JSON | Lightweight project relationship visualization prototype |

See [DATA_PROVENANCE.md](DATA_PROVENANCE.md) for source lineage and [LIMITATIONS.md](LIMITATIONS.md) for deployment boundaries.
