# Production Acceptance Record

**Status:** Acceptance runner implemented; production baseline pending deployment of the runner checkpoint.

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

The baseline results will be added only after Render and Netlify identify the runner checkpoint SHA and the complete automated and rendered-browser manifests have been executed.
