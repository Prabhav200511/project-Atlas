# Production Deployment Operations

## Active backend deployment

- Repository: `Prabhav200511/project-Atlas`
- Branch: `main`
- Deployed commit: `fd45ea4578fdd4eb9e534114584608fb9be024c5`
- Render service: `project-Atlas` (`Python 3`, free instance, Oregon)
- Backend URL: `https://project-atlas-rd7v.onrender.com`
- Verified at: `2026-08-23T12:39:22Z`

Render runs from the repository root with:

```text
Build command: pip install .
Start command: bash ./scripts/start_production.sh
Health-check path: /ready
Auto-deploy: main branch commits
```

## Active frontend deployment

- Repository: `Prabhav200511/project-Atlas`
- Branch: `main`
- Verified deployed commit: `fd45ea4578fdd4eb9e534114584608fb9be024c5`
- Netlify project: `project-atlas-production` (free plan)
- Frontend URL: `https://project-atlas-production.netlify.app`
- Verified at: `2026-08-23T12:39:22Z`

Netlify builds the Next.js application with:

```text
Base directory: frontend
Build command: npm run build
Publish directory: .next
Production branch: main
Environment variable: NEXT_PUBLIC_API_URL=https://project-atlas-rd7v.onrender.com
Auto-publish: main branch commits
```

The unrelated climate-awareness site at `https://project-atlas.netlify.app` was not modified or relinked.

## Environment contract

Secret values must remain in the deployment provider. Never copy them into Git, logs, tickets, or documentation.

The existing service uses these compatible names:

```text
ATLAS_DATABASE_URL
ATLAS_QDRANT_URL
ATLAS_QDRANT_API_KEY
ATLAS_CORS_ORIGINS
GROQ_API_KEY or ATLAS_GEMINI_API_KEY
```

`scripts/start_production.sh` maps the `ATLAS_` compatibility names to the canonical runtime names. New Blueprint deployments use:

```text
DATABASE_URL
QDRANT_URL
QDRANT_API_KEY
FRONTEND_URL
GROQ_API_KEY or GEMINI_API_KEY
```

The explicit non-secret production values are:

```text
ATLAS_ENVIRONMENT=production
WEB_CONCURRENCY=1
FORWARDED_ALLOW_IPS=*
FAST_RERANK=1
FRONTEND_URL=https://project-atlas-production.netlify.app
```

## Recovery performed on 2026-08-23

The original deployment failed during Alembic startup because its Supabase tenant no longer existed. A free Render PostgreSQL instance was provisioned in the same region and the service credential was rotated. Render supplies standard `postgresql://` connection URLs, while Atlas uses SQLAlchemy's asynchronous `asyncpg` driver. Commit `fcf4ce7` added central URL normalization so both migrations and the application use `postgresql+asyncpg://` without changing already-qualified URLs.

After PostgreSQL recovered, `/ready` identified Qdrant as the remaining failed dependency. The configured free Qdrant Cloud cluster was suspended; reactivating the same cluster restored its existing endpoint, API key, and collection. No paid Qdrant resource or replacement credential was created.

The Render health-check path was also corrected from blank to `/ready`, and the explicit non-secret runtime settings above were added. The final configuration deploy repeatedly passed Render's `/ready` check before it became live.

## Verification

Run bounded probes because free services may need a cold start:

```powershell
$AtlasApiUrl = 'https://project-atlas-rd7v.onrender.com'
curl.exe --fail-with-body --max-time 120 "$AtlasApiUrl/health"
curl.exe --fail-with-body --max-time 120 "$AtlasApiUrl/ready"
curl.exe --fail-with-body --max-time 120 "$AtlasApiUrl/docs"
```

Sanitized result at `2026-08-23T12:27:29Z`:

```text
/health  HTTP 200  api=ok
/ready   HTTP 200  api=ok database=ok qdrant=ok
/docs    HTTP 200
```

Frontend and cross-origin verification at `2026-08-23T12:39:22Z`:

```text
Netlify deploy  main@fd45ea4  published in 34 seconds
Frontend        HTTP 200     Project Atlas EPC identity confirmed
Legacy markers  absent       Droughts, Flooding, Global Warming
CORS preflight  HTTP 200     access-control-allow-origin exactly matched the Netlify URL
Verifier        PASS         liveness, readiness, docs, CORS, frontend identity
```

Re-run the same automated checks with:

```powershell
.\.venv\Scripts\python.exe scripts\verify_deployment.py --api-url https://project-atlas-rd7v.onrender.com --frontend-url https://project-atlas-production.netlify.app
```

## Synthetic production canary

Sanitized workflow evidence at `2026-08-23T12:45:12Z`:

```text
Deployed backend SHA: 035ed4b67929b6372a97b8512af8f48a9dfe7f1b
Project name: Atlas Production Canary 2026-08-23-035ed4b
Project ID: 89a77673-9237-49b3-89c8-8f53ed5fd47e
Synthetic documents: 27
Ingestion status: 27 completed, 0 incomplete
Synthetic shipments: 5
Copilot status: PARTIAL
Document-backed citations: 3
```

The canary used only `data/synthetic_epc` through `scripts/seed_demo.py`. The verification record intentionally excludes answer text and source-document content.

The first recovered runtime took about 63 seconds from the start command to Render declaring the service live. The free Render web service can spin down during inactivity, so this deployment does not provide an always-on SLA. The free Qdrant cluster can suspend after inactivity and must be reactivated before readiness returns to `200` again.

The free Render PostgreSQL instance is scheduled to expire on `2026-09-22` unless it is upgraded or replaced. Treat that date as an operational deadline; the database will otherwise be deleted.

## Rollback

For an application regression, open Render **Events**, select the last successful deployment for `fcf4ce7293c0ca447556b723e26edeb6ffae8497`, and choose **Rollback**. Re-run all three probes after rollback.

For a frontend regression, open the Netlify project `project-atlas-production`, select **Deploys**, open the prior successful production deploy for `fd45ea4578fdd4eb9e534114584608fb9be024c5`, and publish that deploy. Re-run frontend identity, CORS preflight, and the automated verifier after rollback. Do not redirect or replace the unrelated `project-atlas.netlify.app` site.

The historical Git baseline `b6d5bb0db10160d368a516f351ae2232701207d7` predates the recovered database configuration and did not produce a healthy Render deployment in this environment. Use it only as a source comparison point, not as an operational rollback target.
