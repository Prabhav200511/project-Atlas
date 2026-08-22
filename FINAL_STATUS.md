# Project Atlas Final QA Status

Feature development remains only partially verified. Deployment verification date: 2026-08-22 (Asia/Kolkata). The canonical branch contains Tasks 1–3 and 5; the paused semantic-runtime work is absent and its measurements are not published here.

## Deployment verification (2026-08-22)

- **Netlify configured target — NOT VERIFIED:** `https://project-atlas.netlify.app/` returned HTTP 200, but visibly served an unrelated climate-awareness “Project ATLAS” site (Droughts/Flooding/Global Warming), not this EPC dashboard.
- **Render configured target — BLOCKED:** `https://project-atlas-rd7v.onrender.com/health` and `/ready` timed out after 60 seconds. A bounded `/health` curl timed out after 120.011 seconds with HTTP `000` and zero bytes. DNS resolution and TCP 443 succeeded, but the service did not produce an HTTP response.
- **Local gates — PASS, not production proof:** backend 101 passed with 3 warnings; frontend 9 passed; lint, typecheck, and production build passed. Source and production ancestry checks also passed, with source `main` remaining `52b7e56`.
- **Deferred:** no production seed/re-upload and no deployed Copilot/RFI semantic proof were run. `evaluation/latest.json` and `evaluation/latest.md` retain their current checked-in pre-semantic results and must not be treated as final semantic or production measurements.

## QA requirements

| Requirement | Status | Evidence |
| --- | --- | --- |
| Backend tests | PASS (local) | `python -m pytest -q`: 101 passed, 3 warnings |
| Backend compile check | PASS | `python3 -m compileall -q app scripts evaluation migrations` |
| Frontend tests | PASS | Vitest: 9 passed across 2 files after executive and Digital Thread integration |
| Frontend lint | PASS | `npm run lint`: no errors or warnings |
| Frontend type check | PASS | `npm run typecheck` |
| Frontend production-mode build | PASS (local) | Next.js production build completed; `/` generated locally |
| Local deployment/startup configuration | PASS (local) | Local configuration/build checks passed; this does not establish that either configured deployment target is live |
| Netlify configured target | NOT VERIFIED | Returned HTTP 200 for an unrelated climate-awareness “Project ATLAS” site on 2026-08-22, not the EPC dashboard |
| Render configured target | BLOCKED | `/health` and `/ready` timed out after 60 seconds; bounded `/health` curl timed out after 120.011 seconds with HTTP `000` / zero bytes |
| Clean database migration | PASS | Alembic upgraded an isolated empty database through `20260721_09`; apply head to existing PostgreSQL before use |
| Evidence-backed Impact Chain | PASS | Focused unit/API tests verify deterministic five-stage propagation, evidence separation, persistence, and project isolation |
| Persisted evaluation dashboard | PASS | Labelled JSON/CSV cases, computed compliance/RAG metrics, failure persistence, project isolation, typed client, and dashboard passed focused tests |
| Supply-chain CSV risk workflow | PASS | Project-scoped persistence, schedule links/float, deterministic exposure, alerts/timelines, DELIVERY_RISK propagation, and dashboard table passed focused tests |
| Counterfactual mitigation calculator | PASS | Three deterministic evidence-backed scenarios, explicit configured/unknown assumptions, persisted selection, recalculated counterfactual chain, and non-mutation regression passed |
| Manual-coordination benchmarks | PASS | Project-scoped measured/projected records, exact hours-saved calculation, synthetic labelling, typed API client, and executive card passed focused tests; no measurements are seeded |
| SWGR-A vertical scenario | PASS | Idempotent integration test verifies cited rating deviation → resubmission → 35-day ETA variance → 28-day exposure → readiness 65→45 → expedite scenario delay 35→17 days |
| Clean seed | PASS | Isolated API/PostgreSQL/Qdrant run ingested 27/27 documents and seeded 5 shipments |
| Current checked-in evaluation | PRE-SEMANTIC / NOT FINAL | Existing `evaluation/latest.json` and `.md` values are preserved; final semantic-runtime and production measurements are deferred |
| UPS-01 end-to-end smoke | PASS (prior local evidence) | Focused `UPS-01` Impact Chain test and isolated clean seed passed using corpus tag `UPS-A` |
| Project isolation | PASS | Query plan, hybrid retrieval, and Equipment Digital Thread cross-project tests passed |
| Invalid API-key/error state | PASS | Invalid Gemini key returns structured 502 `model_gateway_error`; optional compliance/schedule narratives fall back to deterministic output |
| Secret scan: working tree | PASS | No high-confidence secret found outside ignored local files; backend secret names are absent from frontend source/build; rotation not required |
| Secret scan: Git history | NOT APPLICABLE | Search completed; repository has zero commits/history |
| Broken-link scan | PASS | 18 relative Markdown links resolved |
| Mermaid validation | PASS | Mermaid CLI rendered `docs/ARCHITECTURE.mermaid` successfully |
| Local public-artifact files | PASS | README, architecture, pitch content, demo script, checklist, provenance, licenses, limitations, and evaluation reports exist |
| Root project license | FAIL | No owner-approved root `LICENSE` file exists |
| Public repository/link verification | NOT VERIFIED | No committed/public repository URL is available |
| Pitch-deck export | NOT VERIFIED | Presentation content exists; no final PPTX/PDF was found |
| Demo video/public playback | NOT VERIFIED | No final video or signed-out public URL was found |
| Unstop submission | NOT VERIFIED | Requires external form/link verification and submission receipt |

## Required flow verification

Except where explicitly labelled as deployment verification above, the retained API-flow evidence below is prior local/checked-in evidence and must not be read as proof that the current configured Netlify or Render targets work.

| Flow | Status | Evidence |
| --- | --- | --- |
| 1. Ask a cited knowledge question | PASS | Synthetic evaluator asked the UPS autonomy question; citation correctness was 17/17 overall |
| Configured Gemini cited response | NOT VERIFIED | QA intentionally used an invalid key to verify the error path; provider quality was not tested |
| 2. Detect the UPS deviation | PASS (prior local evidence) | Isolated API run returned `NON_COMPLIANT` for the planted UPS voltage deviation |
| 3. Open Equipment Digital Thread | PASS (prior local evidence) | Isolated API run returned the project-scoped `UPS-A` thread; UPS-01 isolation test also passed |
| 4. Display procurement and schedule impact | PASS (prior local evidence) | Isolated API run returned shipment risk and 6 schedule-risk records |
| 5. Recalculate commissioning readiness | PASS (prior local evidence) | Isolated API run recalculated UPS-A readiness as 35/100 for the seeded state |
| 6. Compare mitigation scenarios | PASS (prior local evidence) | Isolated Impact Chain run returned exactly 3 deterministic scenarios |
| 7. Record a human decision | PASS (prior local evidence) | Isolated API run persisted an `APPROVE` action with `ACTION_CREATED` status |
| 8. Show current checked-in evaluation results | PRE-SEMANTIC / NOT FINAL | `evaluation/latest.md`, JSON, and labelled backup evaluation SVG are present; final semantic/production measurements are deferred |

## Measured evaluation snapshot

This is the current checked-in pre-semantic snapshot, not a final semantic-runtime or production measurement. The values are preserved pending authorized semantic evaluation and deployment remediation.

- Compliance: TP/FP/FN/TN 6/0/0/6; precision/recall/F1 1.0/1.0/1.0.
- Synthetic evaluator: 27/27 ingestion, RFI Recall@5 1.0, both expected pairs rank 1, citations 17/17.
- Schedule: one planted case, predicted/simulated delay 35 days, absolute error 0 days.
- Supply chain: 5/5 shipments, 15 supplier tiers, mean alert latency 55 minutes, alternative success 1.0.
- Commissioning: 21/21 steps evaluated, coverage 1.0, expected/actual NCR 1/1.
- Advanced RAG did not beat baseline overall: advanced Recall@12 is 1.0, but current advanced correct-document/page/citation-precision metrics are 0.0.
- Manual effort remains `NOT_MEASURED` until benchmark records are submitted; the dashboard does not seed or infer an hours-saved claim.

## Release-blocking defects fixed during QA

- PostgreSQL evidence inserts now flush their parent ImpactEvent rows first; the vertical scenario regression test runs with SQLite foreign-key enforcement enabled.
- Gemini provider errors previously escaped as unhandled 500 responses.
- Required AI calls now return a safe structured 502; optional deterministic compliance and schedule workflows continue with verified local explanations.
- Regression coverage is in `tests/test_llm_gateway.py`.

## Exact startup commands

One-command local demo:

```bash
cp .env.example .env
# Set GEMINI_API_KEY in .env
./scripts/start_demo.sh
```

Manual startup:

```bash
python3 -m pip install -e '.[dev]'
(cd frontend && npm ci)
docker compose up -d postgres qdrant
alembic upgrade head
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8001
python3 scripts/seed_demo.py --api-url http://localhost:8001 --project-name "Atlas Synthetic Demo"
(cd frontend && NEXT_PUBLIC_API_URL=http://localhost:8001 npm run dev)
```

Evaluation and final validation:

```bash
python3 -m evaluation.run_all
python3 scripts/evaluate_synthetic.py
python3 -m pytest -q
(cd frontend && npm run lint && npm run typecheck && npm test && npm run build)
```

## Remaining blockers

- Remediate the Netlify target so it serves this EPC dashboard, then verify dashboard content and API-origin requests.
- Diagnose and remediate the Render target so `/health` and `/ready` return HTTP 200 before any production seed/re-upload or Copilot/RFI smoke test.
- The semantic-runtime evaluation and its production measurements remain paused/deferred; do not claim final semantic or production results from the current `evaluation/latest.*` files.
- Add an owner-approved root `LICENSE` before public-repository submission.
- Export and verify the final pitch deck; record and verify the public demo video; complete Unstop submission checks.
- Verify one cited response with a valid Gemini key/model before presenting live-provider behavior.
- Atlas local PostgreSQL now binds to host port 55432 to avoid the unrelated service occupying port 5432.
