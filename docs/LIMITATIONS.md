# Limitations

## Security and tenancy

- Authentication, RBAC, tenant quotas, signed URLs, and production tenant isolation are not implemented. `project_id` filters are application-level isolation, not an authorization system.
- Local demo credentials do not exist. The service must not be exposed to the public internet as configured.
- Prompt-injection checks and an untrusted-evidence boundary exist, but adversarial coverage and operational policy tuning are incomplete.
- Upload malware scanning, content disarm, encryption-key management, retention enforcement, and formal audit immutability are not implemented.

## Data and integrations

- All EPC documents, equipment, vendors, costs, dates, shipment events, and requirements are synthetic.
- There is no live AIS, vessel position, carrier, vendor, geospatial, weather, ERP, QMS, or P6 integration. Supply-chain results are explicitly synthetic demo data.
- Computer-vision site evidence is roadmap only.
- Original documents use local filesystem storage, not production object storage.
- NetworkX/JSON is a lightweight prototype, not production graph storage or governed master data.

## AI and retrieval

- Gemini requires a configured API key. Local deterministic embeddings are a prototype and are not managed production embeddings.
- `evaluation/latest.json` and `evaluation/latest.md` are the current checked-in, pre-semantic results. The exact values remain unchanged: advanced Recall@12 is `1.0` versus baseline `0.75`, while advanced correct-document rate, correct-page rate, and citation precision are `0.0`. They do not support an overall advanced-RAG improvement claim and must not be relabelled as final semantic measurements.
- Synthetic evaluation and deterministic test doubles do not measure live Gemini quality, latency, token billing, or production concurrency.
- Final semantic-runtime evaluation and production Copilot/RFI measurements are deferred. No production seed, re-upload, or semantic proof was run during the 2026-08-22 deployment verification.
- Citations reduce unsupported answers but do not replace engineering review. Conflicting or insufficient evidence may still require manual investigation.

## Engineering workflows

- Compliance rules cover the planted schemas and unit conversions; they are not a certified code/standards checker.
- Schedule results are deterministic scenario analysis, not trained historical prediction. The current error result covers one planted 35-day case.
- Commissioning pass/fail and readiness use visible project rules, not certification logic. Electronic signatures and offline/mobile execution are absent.
- Mitigation costs/days are calculated from supplied scenario inputs. They are not quotations, commitments, or approved change orders until a human records a decision.
- The UI’s “UPS-01” walkthrough label maps to the seeded source tag `UPS-A`; this naming mismatch should be disclosed during the demo.

## Operations and evidence

- Upload parsing and indexing run synchronously in the request; there is no background worker or retry queue. Queue-backed ingestion, autoscaling, production observability, backups, disaster recovery, load tests, and SLOs are roadmap work.
- Measured latencies are in-process evaluation-harness values, not deployment SLOs.
- The Netlify URL is a configured target, not a verified Atlas deployment: on 2026-08-22 it returned HTTP 200 for an unrelated climate-awareness “Project ATLAS” site (Droughts/Flooding/Global Warming), rather than this EPC dashboard.
- The Render URL is a configured target, not a verified Atlas API: on 2026-08-22 `/health` and `/ready` timed out after 60 seconds, and a bounded `/health` curl timed out after 120.011 seconds with HTTP `000` and zero bytes. DNS and TCP 443 succeeded, but the service produced no HTTP response.
- Local configuration/build checks passed (backend 101 passed with 3 warnings; frontend 9 passed, lint/typecheck/build passed), but they are not production verification. Deployment remediation and a subsequent full smoke test remain required.
- Manual effort and hours saved are `NOT_MEASURED`.
- A root project license is missing and must be resolved before public-repository publication.
