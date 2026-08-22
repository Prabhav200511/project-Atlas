# Project Atlas Final QA Status

Feature development is frozen. QA date: 2026-07-21 (Asia/Kolkata).

## QA requirements

| Requirement | Status | Evidence |
| --- | --- | --- |
| Backend tests | PASS | `python3 -m pytest -q`: 88 passed, 20 dependency deprecation warnings |
| Backend compile check | PASS | `python3 -m compileall -q app scripts evaluation migrations` |
| Frontend tests | PASS | Vitest: 9 passed across 2 files after executive and Digital Thread integration |
| Frontend lint | PASS | `npm run lint`: no errors or warnings |
| Frontend type check | PASS | `npm run typecheck` |
| Production frontend build | PASS | Next.js production build completed; `/` generated |
| Deployment probes/startup | PASS | `/health` liveness, `/ready` dependency readiness, environment CORS/API URL, and `scripts/start_production.sh` verified |
| Render/Vercel deployment configuration | PASS | `render.yaml`, backend-only generic deployment aliases, Vercel Root Directory instructions, idempotent seed/reset commands, and `DEPLOY.md` validated |
| Clean database migration | PASS | Alembic upgraded an isolated empty database through `20260721_09`; apply head to existing PostgreSQL before use |
| Evidence-backed Impact Chain | PASS | Focused unit/API tests verify deterministic five-stage propagation, evidence separation, persistence, and project isolation |
| Persisted evaluation dashboard | PASS | Labelled JSON/CSV cases, computed compliance/RAG metrics, failure persistence, project isolation, typed client, and dashboard passed focused tests |
| Supply-chain CSV risk workflow | PASS | Project-scoped persistence, schedule links/float, deterministic exposure, alerts/timelines, DELIVERY_RISK propagation, and dashboard table passed focused tests |
| Counterfactual mitigation calculator | PASS | Three deterministic evidence-backed scenarios, explicit configured/unknown assumptions, persisted selection, recalculated counterfactual chain, and non-mutation regression passed |
| Manual-coordination benchmarks | PASS | Project-scoped measured/projected records, exact hours-saved calculation, synthetic labelling, typed API client, and executive card passed focused tests; no measurements are seeded |
| SWGR-A vertical scenario | PASS | Idempotent integration test verifies cited rating deviation → resubmission → 35-day ETA variance → 28-day exposure → readiness 65→45 → expedite scenario delay 35→17 days |
| Clean seed | PASS | Isolated API/PostgreSQL/Qdrant run ingested 27/27 documents and seeded 5 shipments |
| Complete evaluation | PASS | `python3 -m evaluation.run_all` completed and refreshed `evaluation/latest.json` and `.md` |
| UPS-01 end-to-end smoke | PASS | Focused `UPS-01` Impact Chain test passed; live clean seed passed using corpus tag `UPS-A` |
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

| Flow | Status | Evidence |
| --- | --- | --- |
| 1. Ask a cited knowledge question | PASS | Synthetic evaluator asked the UPS autonomy question; citation correctness was 17/17 overall |
| Live Gemini cited response | NOT VERIFIED | QA intentionally used an invalid key to verify the error path; live-provider quality was not tested |
| 2. Detect the UPS deviation | PASS | Clean live API returned `NON_COMPLIANT` for the planted UPS voltage deviation |
| 3. Open Equipment Digital Thread | PASS | Clean live API returned the project-scoped `UPS-A` thread; UPS-01 isolation test also passed |
| 4. Display procurement and schedule impact | PASS | Clean live API returned shipment risk and 6 schedule-risk records |
| 5. Recalculate commissioning readiness | PASS | Clean live API recalculated UPS-A readiness as 35/100 for the seeded state |
| 6. Compare mitigation scenarios | PASS | Clean live Impact Chain returned exactly 3 deterministic scenarios |
| 7. Record a human decision | PASS | Clean live API persisted an `APPROVE` action with `ACTION_CREATED` status |
| 8. Show measured evaluation results | PASS | `evaluation/latest.md`, JSON, and labelled backup evaluation SVG are present and refreshed |

## Measured evaluation snapshot

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

- Add an owner-approved root `LICENSE` before public-repository submission.
- Create a commit, run a full-history secret scan, publish the repository, and verify it signed out.
- Export and verify the final pitch deck; record and verify the public demo video; complete Unstop submission checks.
- Verify one cited response with a valid Gemini key/model before presenting live-provider behavior.
- Atlas local PostgreSQL now binds to host port 55432 to avoid the unrelated service occupying port 5432.
