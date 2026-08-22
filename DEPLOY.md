# Deployment

## Services

- **Vercel:** deploy `frontend` as the project Root Directory. Its browser client calls FastAPI through `NEXT_PUBLIC_API_URL`.
- **Render:** create the API service from [`render.yaml`](render.yaml). Render supplies `PORT`; the start command runs `alembic upgrade head` before `uvicorn app.main:app --port $PORT`.
- **Supabase:** provide the PostgreSQL connection as an async SQLAlchemy `DATABASE_URL` (`postgresql+asyncpg://…`); Supabase Auth and Storage variables remain backend-only.
- **Qdrant Cloud:** configure `QDRANT_URL` and `QDRANT_API_KEY` on Render.
- **Gemini:** FastAPI reads `GEMINI_API_KEY` and `GEMINI_MODEL`; the frontend never calls Gemini directly.

## Environment variables

| Service | Variables |
| --- | --- |
| Render backend | `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `QDRANT_URL`, `QDRANT_API_KEY`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `FRONTEND_URL` |
| Vercel frontend | `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` |

Never put `DATABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `QDRANT_API_KEY`, or `GEMINI_API_KEY` in Vercel. Do not create `NEXT_PUBLIC_GEMINI_API_KEY`.

## Deploy commands

```bash
# Render start command (configured by render.yaml)
./scripts/start_production.sh

# Run migrations manually, if needed
alembic upgrade head

# Seed or refresh the idempotent synthetic demo after the API is healthy
python3 scripts/seed_demo.py --api-url "$NEXT_PUBLIC_API_URL"

# Reset the project-scoped synthetic state, then restore the vertical scenario
curl -fsS -X POST "$NEXT_PUBLIC_API_URL/projects/$PROJECT_ID/demo/reset"
curl -fsS -X POST "$NEXT_PUBLIC_API_URL/projects/$PROJECT_ID/demo/vertical-scenario"
```

Vercel auto-detects Next.js when its Root Directory is `frontend`; no `vercel.json` is needed. Set `NEXT_PUBLIC_API_URL` to the Render API URL before building. Set `FRONTEND_URL` on Render to the matching Vercel URL so FastAPI permits only that browser origin.

## Release check

After deployment, request `GET /health` for liveness and `GET /ready` for database/Qdrant readiness. Project filters are enforced by the existing project-scoped API services; deploy behind an authenticated gateway until application authentication is enabled.
