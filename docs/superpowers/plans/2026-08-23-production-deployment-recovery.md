# Production Deployment Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy the current Project Atlas `main` branch to a verified Render backend and Netlify frontend, prove the complete synthetic evidence workflow in production, and record an exact rollback point without introducing multi-provider routing changes.

**Architecture:** Preserve the current FastAPI, PostgreSQL, Qdrant, Groq/Gemini, and Next.js architecture. Diagnose the existing Render target before changing code, automate read-only production checks, create a new Netlify site if the existing site is linked to unrelated content, and enable production only after API, ingestion, Copilot, CORS, and rendered-browser gates pass.

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, Alembic, PostgreSQL/Supabase, Qdrant Cloud, Next.js 16, Netlify OpenNext adapter, Render Blueprint, pytest, Vitest, httpx.

**Spec:** `docs/superpowers/specs/2026-08-23-deployment-first-multi-provider-routing-design.md`

## Global Constraints

- Production source is `https://github.com/Prabhav200511/project-Atlas`, branch `main`.
- The baseline commit before this plan is `b6d5bb0db10160d368a516f351ae2232701207d7`.
- Do not implement the six-provider router in this plan.
- Do not change `evaluation/latest.md` or `evaluation/latest.json`.
- Do not commit API keys, database credentials, provider responses, or prompt/document content.
- Use only the repository's synthetic corpus for production smoke tests.
- Treat `/health` as liveness and `/ready` as dependency readiness.
- Do not overwrite the unrelated application currently served at `https://project-atlas.netlify.app`; create a new Atlas EPC site unless ownership and replacement authorization are independently confirmed.
- Keep `WEB_CONCURRENCY=1` because uploads and the NetworkX graph use process-local filesystem state.
- A production claim requires fresh HTTP probes, API workflow evidence, and rendered-browser verification from the deployed SHA.
- If a gate fails, record the failure truthfully and stop promotion; do not weaken the gate.

---

### Task 1: Establish the Local and Deployment Contract Baseline

**Files:**
- Create: `tests/test_deployment_contract.py`
- Modify only if the RED test proves drift: `render.yaml`
- Modify only if the RED test proves drift: `netlify.toml`
- Test: `tests/test_deployment_contract.py`

**Interfaces:**
- Consumes: repository-root `render.yaml`, `netlify.toml`, `scripts/start_production.sh`, and `frontend/package.json`.
- Produces: executable assertions for the intended repository deployment contract.

- [ ] **Step 1: Confirm the exact baseline and a clean worktree**

Run:

```powershell
git fetch origin
git rev-parse HEAD
git rev-parse origin/main
git status --short
```

Expected: local and `origin/main` resolve to the same SHA, `git merge-base --is-ancestor ec4f2bf327174f3758862e8b2c996e13807801eb HEAD` exits `0`, and status is empty. The product rollback baseline remains `b6d5bb0db10160d368a516f351ae2232701207d7`.

- [ ] **Step 2: Write the deployment contract test**

Create `tests/test_deployment_contract.py`:

```python
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_render_blueprint_has_atlas_production_contract() -> None:
    blueprint = (ROOT / "render.yaml").read_text()

    for required in (
        "name: project-atlas-api",
        "runtime: python",
        "buildCommand: pip install .",
        "startCommand: bash ./scripts/start_production.sh",
        "healthCheckPath: /ready",
        "key: DATABASE_URL",
        "key: QDRANT_URL",
        "key: QDRANT_API_KEY",
        "key: FRONTEND_URL",
    ):
        assert required in blueprint


def test_netlify_builds_the_nextjs_application_from_frontend() -> None:
    config = (ROOT / "netlify.toml").read_text()

    assert 'base = "frontend"' in config
    assert 'command = "npm run build"' in config
    assert 'publish = ".next"' in config
    assert 'package = "@netlify/plugin-nextjs"' in config


def test_production_start_binds_render_port_and_requires_dependencies() -> None:
    script = (ROOT / "scripts" / "start_production.sh").read_text()

    for required in (
        ': "${DATABASE_URL:?DATABASE_URL is required}"',
        ': "${QDRANT_URL:?QDRANT_URL is required}"',
        ': "${QDRANT_API_KEY:?QDRANT_API_KEY is required}"',
        ': "${FRONTEND_URL:?FRONTEND_URL is required}"',
        ': "${PORT:?PORT is required}"',
        "alembic upgrade head",
        "--host 0.0.0.0",
        '--port "$PORT"',
        '--workers "${WEB_CONCURRENCY:-1}"',
    ):
        assert required in script
```

- [ ] **Step 3: Run the contract test and classify any failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_deployment_contract.py -q
```

Expected: PASS. A failure means repository deployment configuration drift; update only the asserted contract field, then rerun until PASS.

- [ ] **Step 4: Run local backend and frontend gates**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
Push-Location frontend
npm.cmd test
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run build
Pop-Location
```

Expected: all commands exit `0`. Preserve the exact pass counts in the task report.

- [ ] **Step 5: Commit the deployment contract test**

```powershell
git add tests/test_deployment_contract.py render.yaml netlify.toml
git diff --cached --check
git commit -m "test: codify production deployment contract"
git push origin HEAD:main
```

Expected: the commit is present on `origin/main` and the worktree is clean.

---

### Task 2: Add a Read-Only Production Verification Command

**Files:**
- Create: `scripts/verify_deployment.py`
- Create: `tests/test_verify_deployment.py`
- Modify: `README.md`
- Test: `tests/test_verify_deployment.py`

**Interfaces:**
- Consumes: `verify(api_url: str, frontend_url: str, client: httpx.Client) -> list[CheckResult]`.
- Produces: `CheckResult(name: str, ok: bool, detail: str)` and a CLI that exits `0` only when backend liveness, readiness, docs, CORS, and frontend identity pass.

- [ ] **Step 1: Write failing verifier tests**

Create `tests/test_verify_deployment.py` with an `httpx.MockTransport` that proves:

```python
import httpx

from scripts.verify_deployment import verify


def test_verify_accepts_the_atlas_backend_and_frontend() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "components": {"api": "ok"}})
        if request.url.path == "/ready":
            return httpx.Response(
                200,
                json={"status": "ok", "components": {"api": "ok", "database": "ok", "qdrant": "ok"}},
            )
        if request.url.path == "/docs":
            return httpx.Response(200, text="<title>Project Atlas - Swagger UI</title>")
        if request.method == "OPTIONS" and request.url.path == "/projects":
            return httpx.Response(200, headers={"access-control-allow-origin": "https://atlas-epc.netlify.app"})
        return httpx.Response(200, text="<h1>Project Atlas</h1><p>EPC project intelligence</p>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = verify("https://api.example", "https://atlas-epc.netlify.app", client)

    assert all(result.ok for result in results)


def test_verify_rejects_the_unrelated_climate_site_and_degraded_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "components": {"api": "ok"}})
        if request.url.path == "/ready":
            return httpx.Response(503, json={"status": "degraded", "components": {"database": "error"}})
        if request.url.path == "/docs":
            return httpx.Response(200, text="Swagger UI")
        if request.method == "OPTIONS":
            return httpx.Response(200)
        return httpx.Response(200, text="Droughts Flooding Global Warming")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = verify("https://api.example", "https://atlas-epc.netlify.app", client)

    assert {result.name for result in results if not result.ok} == {"readiness", "cors", "frontend_identity"}
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_verify_deployment.py -q
```

Expected: FAIL with `ModuleNotFoundError` or missing `verify`.

- [ ] **Step 3: Implement the minimal read-only verifier**

Create `scripts/verify_deployment.py` with:

```python
import argparse
import json
from dataclasses import asdict, dataclass

import httpx


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def verify(api_url: str, frontend_url: str, client: httpx.Client) -> list[CheckResult]:
    api_url = api_url.rstrip("/")
    frontend_url = frontend_url.rstrip("/")
    results: list[CheckResult] = []

    health = client.get(f"{api_url}/health", timeout=20)
    health_body = health.json() if health.headers.get("content-type", "").startswith("application/json") else {}
    results.append(CheckResult("liveness", health.status_code == 200 and health_body.get("status") == "ok", str(health.status_code)))

    ready = client.get(f"{api_url}/ready", timeout=20)
    ready_body = ready.json() if ready.headers.get("content-type", "").startswith("application/json") else {}
    components = ready_body.get("components", {})
    ready_ok = ready.status_code == 200 and all(components.get(name) == "ok" for name in ("api", "database", "qdrant"))
    results.append(CheckResult("readiness", ready_ok, json.dumps(components, sort_keys=True)))

    docs = client.get(f"{api_url}/docs", timeout=20)
    results.append(CheckResult("docs", docs.status_code == 200 and "Swagger UI" in docs.text, str(docs.status_code)))

    cors = client.options(
        f"{api_url}/projects",
        headers={"Origin": frontend_url, "Access-Control-Request-Method": "GET"},
        timeout=20,
    )
    results.append(CheckResult("cors", cors.headers.get("access-control-allow-origin") == frontend_url, cors.headers.get("access-control-allow-origin", "missing")))

    frontend = client.get(frontend_url, timeout=20)
    identity_ok = frontend.status_code == 200 and "Project Atlas" in frontend.text and "EPC project intelligence" in frontend.text
    results.append(CheckResult("frontend_identity", identity_ok, str(frontend.status_code)))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the deployed Project Atlas identity and dependencies.")
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--frontend-url", required=True)
    args = parser.parse_args()
    with httpx.Client(follow_redirects=True) as client:
        results = verify(args.api_url, args.frontend_url, client)
    print(json.dumps([asdict(result) for result in results], indent=2))
    raise SystemExit(0 if all(result.ok for result in results) else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run focused and full tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_verify_deployment.py tests\test_deployment_contract.py tests\test_health.py tests\test_config.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all tests PASS.

- [ ] **Step 5: Document the verifier without claiming production success**

Add this command to the README deployment section:

```powershell
python scripts/verify_deployment.py --api-url https://project-atlas-rd7v.onrender.com --frontend-url https://atlas-epc.netlify.app
```

State that the example frontend hostname is valid only after Netlify assigns or confirms it; keep the existing `BLOCKED` and `NOT VERIFIED` labels at this stage.

- [ ] **Step 6: Commit and push**

```powershell
git add scripts/verify_deployment.py tests/test_verify_deployment.py README.md
git diff --cached --check
git commit -m "test: automate production deployment verification"
git push origin HEAD:main
```

---

### Task 3: Diagnose and Recover the Render Backend

**Files:**
- Modify if diagnostics require repository correction: `render.yaml`
- Modify if diagnostics require startup correction: `scripts/start_production.sh`
- Modify if diagnostics prove a runtime compatibility issue: `.python-version`
- Test if application behavior changes: `tests/test_deployment_contract.py`
- Create: `docs/operations/production-deployment.md`

**Interfaces:**
- Consumes: Render service events/build/runtime logs and secret-name inventory; never copies secret values.
- Produces: a responding backend URL whose `/health`, `/ready`, and `/docs` checks pass at the deployed SHA.

- [ ] **Step 1: Open the Render service and verify source identity**

In the signed-in Render dashboard, inspect `project-atlas-api` and record only these non-secret fields in an ignored task report:

- Repository must be `Prabhav200511/project-Atlas`.
- Branch must be `main`.
- Root directory must be repository root.
- Runtime must be Python.
- Build command must be `pip install .` unless the deploy log proves a packaging failure.
- Start command must be `bash ./scripts/start_production.sh`.
- Health path must be `/ready`.
- Deployed commit must equal current `origin/main`.

If the existing service points to unrelated source, do not overwrite it. Create a new Blueprint-backed service from this repository and record the new Render URL.

- [ ] **Step 2: Inspect the latest failed or hanging deploy at the failure boundary**

Classify the first failing boundary using this decision table:

| Evidence | Required action |
|---|---|
| Build log reports unsupported Python/dependency wheel | Reproduce with the logged Python version locally; write a failing contract assertion; add `.python-version` containing the exact passing Python minor version; redeploy. |
| Startup log stops before `alembic upgrade head` | Correct missing required secret names in Render; do not change code. |
| Startup log stops during Alembic | Validate the Supabase connection outside Render with `alembic current`; use the Supabase runtime-compatible connection string; redeploy. |
| Startup completes but `/ready` reports database error | Correct `DATABASE_URL`; preserve `/health` and `/ready` semantics. |
| Startup completes but `/ready` reports Qdrant error | Correct `QDRANT_URL` or `QDRANT_API_KEY`; verify the configured collection compatibility before redeploy. |
| Startup exits because no model key exists | Configure either `GROQ_API_KEY` or `GEMINI_API_KEY`; never commit the value. |
| Application exception is reproducible locally | Add the smallest failing pytest regression, implement the minimal fix, and run the full suite before redeploy. |
| Service is healthy only after a free-tier cold start | Record cold-start duration; keep bounded probes at 120 seconds; do not claim an always-on SLA. |

- [ ] **Step 3: Verify the required secret-name inventory**

The Render Environment page must contain non-empty values for:

```text
DATABASE_URL
QDRANT_URL
QDRANT_API_KEY
FRONTEND_URL
GROQ_API_KEY or GEMINI_API_KEY
```

Set these non-secret operational values:

```text
ATLAS_ENVIRONMENT=production
WEB_CONCURRENCY=1
FORWARDED_ALLOW_IPS=*
FAST_RERANK=1
```

Do not expose or copy secret values into tool output, task reports, commits, or chat.

- [ ] **Step 4: Apply only the diagnosed repository fix**

If Task 3 Step 2 identifies a repository defect, first make its focused test fail, then patch only `render.yaml`, `scripts/start_production.sh`, `.python-version`, or the proven application file. Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_deployment_contract.py tests\test_health.py tests\test_config.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: focused and full suites PASS before pushing.

- [ ] **Step 5: Deploy and poll bounded backend probes**

After Render reports the deployed SHA, run:

```powershell
$AtlasApiUrl = 'https://project-atlas-rd7v.onrender.com'
curl.exe --fail-with-body --max-time 120 "$AtlasApiUrl/health"
curl.exe --fail-with-body --max-time 120 "$AtlasApiUrl/ready"
curl.exe --fail-with-body --max-time 120 "$AtlasApiUrl/docs"
```

If a new service URL was created, assign that exact URL to `$AtlasApiUrl` and record it in `docs/operations/production-deployment.md`.

Expected: HTTP `200`; readiness components are exactly `api`, `database`, and `qdrant`, all `ok`.

- [ ] **Step 6: Write the backend operations record**

Create `docs/operations/production-deployment.md` with:

- Repository, branch, deployed SHA, Render URL, and UTC verification timestamp.
- Non-secret environment-variable names.
- Exact build/start/health configuration.
- Root cause and applied correction.
- Probe commands and sanitized results.
- Free-tier cold-start limitation if applicable.
- Rollback instruction: select the last successful deploy for baseline SHA `b6d5bb0db10160d368a516f351ae2232701207d7` in Render Events and choose rollback.

- [ ] **Step 7: Commit any diagnosed fix and the operations record**

```powershell
git add render.yaml scripts/start_production.sh .python-version tests/test_deployment_contract.py docs/operations/production-deployment.md
git diff --cached --check
git commit -m "fix: restore Render production deployment"
git push origin HEAD:main
```

Omit nonexistent or unchanged paths from `git add`. If no repository fix was required, commit only the operations record with message `docs: record Render deployment recovery`.

---

### Task 4: Deploy the Correct Next.js Frontend on Netlify

**Files:**
- Modify only if production build proves drift: `netlify.toml`
- Modify only if API resolution proves drift: `frontend/src/lib/api.ts`
- Test if code changes: `frontend/src/lib/api.test.ts`
- Modify: `docs/operations/production-deployment.md`

**Interfaces:**
- Consumes: the verified Render URL from Task 3 and root `netlify.toml`.
- Produces: a Netlify production URL serving this repository's EPC dashboard with `NEXT_PUBLIC_API_URL` set to the verified Render URL.

- [ ] **Step 1: Preserve the unrelated existing site**

Open `https://project-atlas.netlify.app` and confirm whether it still serves the climate-awareness application. If it does, do not relink or overwrite that site. In Netlify, create a new site from `Prabhav200511/project-Atlas` and branch `main`.

- [ ] **Step 2: Verify the Netlify build contract**

Configure or confirm:

```text
Base directory: frontend
Build command: npm run build
Publish directory: .next
Production branch: main
Environment variable name: NEXT_PUBLIC_API_URL
Environment variable value: the exact `$AtlasApiUrl` value verified in Task 3
```

The root `netlify.toml` supplies the first three values and takes precedence over conflicting UI settings. Store the API URL in Netlify configuration, not source code.

- [ ] **Step 3: Run the production frontend gate locally**

Run with the exact verified Render URL:

```powershell
$env:NEXT_PUBLIC_API_URL = $AtlasApiUrl
Push-Location frontend
npm.cmd test
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run build
Pop-Location
Remove-Item Env:NEXT_PUBLIC_API_URL
```

Expected: tests, lint, typecheck, and build all exit `0`.

- [ ] **Step 4: Deploy and verify source identity**

Trigger the Netlify production deploy from current `main`. Record the assigned production URL as `$AtlasFrontendUrl`. Confirm the deploy detail shows the same SHA as `origin/main`.

Run:

```powershell
curl.exe --fail-with-body --max-time 60 $AtlasFrontendUrl
```

Expected: HTTP `200`, HTML contains `Project Atlas` and `EPC project intelligence`, and does not contain `Droughts`, `Flooding`, or `Global Warming`.

- [ ] **Step 5: Verify production CORS**

Set Render `FRONTEND_URL` to the exact `$AtlasFrontendUrl` value without a trailing slash and redeploy if the value changed. Then run:

```powershell
curl.exe --fail-with-body --max-time 60 -X OPTIONS "$AtlasApiUrl/projects" -H "Origin: $AtlasFrontendUrl" -H "Access-Control-Request-Method: GET" -i
```

Expected: `access-control-allow-origin` exactly equals `$AtlasFrontendUrl`.

- [ ] **Step 6: Run the automated deployment verifier**

```powershell
.\.venv\Scripts\python.exe scripts\verify_deployment.py --api-url $AtlasApiUrl --frontend-url $AtlasFrontendUrl
```

Expected: every check has `"ok": true` and the process exits `0`.

- [ ] **Step 7: Record and commit the frontend deployment**

Update `docs/operations/production-deployment.md` with the Netlify URL, linked repository, branch, deployed SHA, build settings, CORS result, and rollback procedure. Commit only if tracked content changed:

```powershell
git add netlify.toml frontend/src/lib/api.ts frontend/src/lib/api.test.ts docs/operations/production-deployment.md
git diff --cached --check
git commit -m "docs: record Netlify production deployment"
git push origin HEAD:main
```

---

### Task 5: Prove Production Ingestion and Copilot with Synthetic Data

**Files:**
- Modify only if a defect is reproduced: the smallest affected `app/` or `scripts/` file.
- Test if a defect is reproduced: the corresponding focused `tests/test_*.py` file.
- Modify: `docs/operations/production-deployment.md`

**Interfaces:**
- Consumes: verified `$AtlasApiUrl`, repository synthetic corpus, and `scripts/seed_demo.py`.
- Produces: one production canary project with completed ingestion and a citation-bearing Copilot response.

- [ ] **Step 1: Create an unambiguous canary name**

Use the deployed short SHA:

```powershell
$DeployedSha = (git rev-parse --short origin/main).Trim()
$CanaryName = "Atlas Production Canary 2026-08-23-$DeployedSha"
```

- [ ] **Step 2: Seed only the synthetic corpus**

```powershell
.\.venv\Scripts\python.exe scripts\seed_demo.py --api-url $AtlasApiUrl --project-name $CanaryName
```

Expected: exit `0`, every upload reports completed ingestion, and the command prints the created project UUID.

- [ ] **Step 3: Resolve the canary project and verify document state**

Run a short read-only Python probe:

```powershell
$env:ATLAS_SMOKE_API = $AtlasApiUrl
$env:ATLAS_SMOKE_PROJECT = $CanaryName
@'
import os
import httpx

api = os.environ["ATLAS_SMOKE_API"]
name = os.environ["ATLAS_SMOKE_PROJECT"]
with httpx.Client(base_url=api, timeout=120) as client:
    projects = client.get("/projects").raise_for_status().json()
    project = next(item for item in projects if item["name"] == name)
    documents = client.get(f"/projects/{project['id']}/documents").raise_for_status().json()
    assert documents and all(item["status"] == "completed" for item in documents)
    print(project["id"], len(documents))
'@ | .\.venv\Scripts\python.exe -
```

Expected: a project UUID and nonzero document count.

- [ ] **Step 4: Verify a grounded Copilot response**

Run:

```powershell
@'
import os
import httpx

api = os.environ["ATLAS_SMOKE_API"]
name = os.environ["ATLAS_SMOKE_PROJECT"]
with httpx.Client(base_url=api, timeout=120) as client:
    project = next(item for item in client.get("/projects").raise_for_status().json() if item["name"] == name)
    response = client.post(
        f"/projects/{project['id']}/copilot",
        json={"question": "What short-circuit rating is required for SWGR-A?", "history": []},
    ).raise_for_status().json()
    assert response["status"] in {"ANSWERED", "PARTIAL"}
    assert response["citations"]
    assert all(item["document_id"] for item in response["citations"])
    print(response["status"], len(response["citations"]))
'@ | .\.venv\Scripts\python.exe -
Remove-Item Env:ATLAS_SMOKE_API
Remove-Item Env:ATLAS_SMOKE_PROJECT
```

Expected: `ANSWERED` or `PARTIAL` and at least one citation. Do not print the complete answer or source content into deployment logs.

- [ ] **Step 5: Diagnose any production-only failure before changing code**

For a failure, reproduce it locally with the same synthetic file or request, add the smallest failing regression, implement the minimal correction, run the focused suite and full suite, then redeploy from a separate commit. Never patch production without a local RED/GREEN proof.

- [ ] **Step 6: Record the sanitized workflow evidence**

Update `docs/operations/production-deployment.md` with canary project ID, document count, ingestion statuses, Copilot final status, citation count, deployed SHA, and UTC timestamp. Do not record answer text or document content.

---

### Task 6: Verify the Rendered Production Dashboard

**Files:**
- Modify only if a UI defect is reproduced: `frontend/src/components/dashboard.tsx` or `frontend/src/lib/api.ts`.
- Test if a UI defect is reproduced: `frontend/src/components/dashboard.test.tsx` or `frontend/src/lib/api.test.ts`.
- Modify: `docs/operations/production-deployment.md`

**Interfaces:**
- Consumes: `$AtlasFrontendUrl`, canary project from Task 5, and browser automation.
- Produces: rendered evidence that the EPC dashboard—not the unrelated climate site—is connected to the verified API.

- [ ] **Step 1: Open the production frontend in the in-app browser**

Navigate to `$AtlasFrontendUrl`, wait for the initial load, and take a fresh DOM snapshot.

Expected visible identity:

```text
Project Atlas
EPC project intelligence
Synthetic demo data
API connected
Mitigation calculator
Synthetic supply-chain demo
```

- [ ] **Step 2: Verify the truth and safety labels**

Expected visible text includes:

```text
AI outputs are evidence-led suggestions.
No live carrier, AIS, vendor, ERP, or position feed is connected.
```

Expected absent text includes climate-site navigation or claims involving droughts, flooding, or global warming.

- [ ] **Step 3: Select the canary project and exercise the read path**

Select `$CanaryName` from the project combobox. Confirm document count is nonzero, service health displays `api: ok`, and the executive summary loads without an error notice.

- [ ] **Step 4: Exercise Copilot through the UI**

Open `Knowledge / RFI`, ask `What short-circuit rating is required for SWGR-A?`, and verify the response is labelled `AI evidence response`, includes `Suggestion · not approved`, and displays at least one citation.

- [ ] **Step 5: Inspect browser and service diagnostics**

Check browser console errors, failed network requests, Render runtime logs, and Netlify function/build logs for the smoke-test interval. Zero unhandled frontend errors and zero backend `500` responses are required.

- [ ] **Step 6: If a UI defect exists, prove RED/GREEN and redeploy**

Add a focused Vitest regression, run it RED, implement the minimal fix, then run:

```powershell
Push-Location frontend
npm.cmd test
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run build
Pop-Location
```

Commit the fix separately, push `main`, wait for both deployment targets to reach the new SHA, and repeat Tasks 4–6.

---

### Task 7: Publish Truthful Production Status and Rollback Evidence

**Files:**
- Modify: `README.md`
- Modify: `docs/operations/production-deployment.md`
- Modify only when regenerating is explicitly requested: `scripts/generate_submission_pdf.py`

**Interfaces:**
- Consumes: successful results from Tasks 3–6.
- Produces: public deployment links, verification timestamps, limitations, and rollback instructions tied to one SHA.

- [ ] **Step 1: Run the final exact-tree local gate**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
Push-Location frontend
npm.cmd test
npm.cmd run lint
npm.cmd run typecheck
npm.cmd run build
Pop-Location
git diff --check
```

Expected: all commands exit `0`.

- [ ] **Step 2: Run the final production gate**

```powershell
.\.venv\Scripts\python.exe scripts\verify_deployment.py --api-url $AtlasApiUrl --frontend-url $AtlasFrontendUrl
curl.exe --fail-with-body --max-time 120 "$AtlasApiUrl/ready"
curl.exe --fail-with-body --max-time 60 $AtlasFrontendUrl
```

Expected: all commands exit `0`, readiness is fully `ok`, and frontend identity is Atlas EPC.

- [ ] **Step 3: Update README status only from fresh evidence**

Replace the `BLOCKED` and `NOT VERIFIED` deployment rows with:

- Exact verified Render and Netlify URLs.
- Deployed commit SHA.
- UTC verification timestamp.
- Backend health/readiness result.
- Synthetic ingestion and citation-bearing Copilot smoke result.
- Honest free-tier cold-start and synchronous-ingestion limitations.

If any production gate remains red, retain the blocked wording and record the exact failed boundary instead of claiming success.

- [ ] **Step 4: Complete the rollback section**

Record:

- Baseline rollback SHA `b6d5bb0db10160d368a516f351ae2232701207d7`.
- Last verified deployment SHA.
- Render rollback procedure from the Events page.
- Netlify rollback procedure from Deploys by publishing the prior successful deploy.
- Required post-rollback `/health`, `/ready`, CORS, frontend identity, and browser checks.

- [ ] **Step 5: Commit and push the verified status**

```powershell
git add README.md docs/operations/production-deployment.md
git diff --cached --check
git commit -m "docs: verify production deployment"
git push origin HEAD:main
```

- [ ] **Step 6: Verify repository and production convergence**

```powershell
git fetch origin
git rev-parse HEAD
git rev-parse origin/main
git status --short
```

Expected: local and remote SHAs match, worktree is clean, Render and Netlify both report that SHA, and all final production gates remain green.

## Execution Notes

- Use Render and Netlify dashboards through existing signed-in sessions. Do not request or print credentials.
- External configuration changes are followed immediately by readback and bounded probes.
- Any repository defect discovered during deployment gets its own RED/GREEN commit before the deployment-status commit.
- Do not proceed to the provider-routing implementation plan until this deployment recovery plan has either completed successfully or produced a precise external blocker report.
