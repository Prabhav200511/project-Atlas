# Production Acceptance Record

**Status:** Baseline captured on `d93d8b1`: 74 checks passed and 2 semantic checks failed.

**Production API:** `https://project-atlas-rd7v.onrender.com`

**Production frontend:** `https://project-atlas-production.netlify.app`

## Safety Boundary

- State-changing checks require `--allow-synthetic-mutations`.
- The project name must begin with `Atlas Production Canary` and match the exact requested name.
- Existing customer or similarly named projects are never selected as a fallback.
- The canary project remains in production because the current API has no project-delete endpoint.
- Reports exclude prompts, completions, document content, secret values, raw provider messages, and raw response bodies.

## Coverage

The automated manifest covers liveness, readiness, OpenAPI, CORS, frontend identity, projects, documents, ingestion, retrieval, context, Copilot, query planning, RFI matching, graph, compliance, schedule, commissioning, procurement, supply-chain workflows, executive summary, digital thread, both impact-chain surfaces, evaluation, mitigation, benchmarks, demo reset, and the vertical scenario.

Rendered-browser acceptance separately covers the dashboard's overview, upload, Copilot/RFI, compliance, evidence, digital-thread, impact-chain, commissioning, supply-chain, mitigation, and evaluation surfaces plus console and network errors.

## Command

```powershell
$Sha = (git rev-parse --short HEAD).Trim()
$Canary = "Atlas Production Canary 2026-08-23-$Sha"
.\.venv\Scripts\python.exe scripts\production_acceptance.py `
  --api-url https://project-atlas-rd7v.onrender.com `
  --frontend-url https://project-atlas-production.netlify.app `
  --project-name $Canary `
  --deployed-sha $Sha `
  --allow-synthetic-mutations `
  --output .superpowers/sdd/2026-08-23-production-acceptance-router/baseline.json
```

## Baseline: 2026-08-23

- Render and Netlify both identified deployed commit `d93d8b1` before the run.
- Public liveness, readiness, API docs, CORS, and frontend identity probes passed.
- The guarded runner created `Atlas Production Canary 2026-08-23-d93d8b1`; the project remains because the API has no project-delete operation.
- Automated result: **74 PASS, 2 FAIL, 0 BLOCKED** across all 49 deployed OpenAPI operations and the supporting ingestion/document checks.
- `copilot_grounding` returned HTTP 200 but failed the grounding contract.
- `evaluation_run` returned HTTP 201 but failed the evaluation-result contract; the persisted evaluation read still passed.
- All 11 major dashboard views rendered for the exact canary project: overview, Knowledge/RFI, equipment thread, compliance, impact chain, mitigation, commissioning, synthetic supply chain, evidence, evaluation, and documents.
- The browser console contained no warnings or errors while traversing those views.

The sanitized machine-readable baseline is intentionally stored under ignored `.superpowers/sdd/` evidence storage. It excludes prompts, completions, document content, secrets, provider messages, and raw response bodies.
