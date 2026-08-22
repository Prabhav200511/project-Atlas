# API reference

The running service provides interactive OpenAPI documentation at `/docs`. Every document, retrieval, finding, record, and graph endpoint is scoped by the path `project_id`.

| Area | Endpoint | Purpose |
| --- | --- | --- |
| Liveness | `GET /health` | API process status. |
| Readiness | `GET /ready` | API, PostgreSQL, and Qdrant readiness. |
| Projects | `POST /projects`, `GET /projects` | Create and select a project. |
| Documents | `GET/POST /projects/{project_id}/documents` | List project documents or upload, parse, and index one synchronously in this request; no background worker or retry queue runs. |
| Ingestion | `GET /projects/{project_id}/documents/{document_id}/ingestion` | Return the completed or failed status from that synchronous ingestion. |
| Evidence | `POST /projects/{project_id}/retrieve` | QueryPlan-filtered dense/BM25 retrieval fused by RRF, with chunk ranks and citations. |
| Generation context | `POST /projects/{project_id}/context` | Reranked, diverse, revision-aware, token-bounded `ContextBundle`. |
| Knowledge | `POST /projects/{project_id}/copilot` | Grounded answer with verified claim status, conflicts, exact-span citations, and a content-safe workflow trace (latency, chunk IDs, tokens, retry, status). Copilot continues through knowledge retrieval; it does not execute a recommended service endpoint. |
| Query plan | `POST /projects/{project_id}/query-plan` | Structured query rewrite, intent, filters, and an existing suggested destination. `execution_mode` is always `advisory_only`: this endpoint classifies and recommends, but does not dispatch or execute the suggested endpoint. |
| RFI | `POST /projects/{project_id}/rfis/matches` | Ranked “possible previous match” results. |
| Compliance | `POST /projects/{project_id}/compliance/checks` | Compare one specification and one submittal. |
| Review | `PATCH /projects/{project_id}/compliance/findings/{finding_id}/review` | Persist approved, rejected, or needs-review action with an optional reviewer note. |
| Compliance metrics | `GET /projects/{project_id}/compliance/evaluation` | Synthetic ground-truth TP, FP, FN, TN, precision, recall, and F1. |
| Schedule | `POST /projects/{project_id}/schedule/analysis` | Scenario-based risk, float, chain, and evidence. |
| Commissioning | `GET /projects/{project_id}/commissioning/procedures/{document_id}` | Retrieve ordered procedure steps. |
| Test record | `POST /projects/{project_id}/commissioning/records` | Store observations, coverage, and non-conformances. |
| Knowledge graph | `GET /projects/{project_id}/graph` | Project-scoped NetworkX JSON export. |
| Equipment digital thread | `GET /projects/{project_id}/equipment/{equipment_id}/digital-thread` | Relational equipment summary, current approved documents, requirements, findings, RFIs, vendor/shipments, schedule, commissioning/NCR, mitigations, and evidence links. |
| Procurement | `POST /projects/{project_id}/procurement/dashboard` | Explicit demo mock; no live AIS/geospatial data. |

Failures use the structured shape `{ "error": { "code", "message", "details"? } }`. Uploads accept multipart fields `document_type` and `file`; schedules must be CSV.
