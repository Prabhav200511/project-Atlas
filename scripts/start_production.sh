#!/usr/bin/env bash
set -euo pipefail

export DATABASE_URL="${DATABASE_URL:-${ATLAS_DATABASE_URL:-}}"
export QDRANT_URL="${QDRANT_URL:-${ATLAS_QDRANT_URL:-}}"
export QDRANT_API_KEY="${QDRANT_API_KEY:-${ATLAS_QDRANT_API_KEY:-}}"
export FRONTEND_URL="${FRONTEND_URL:-${ATLAS_FRONTEND_URL:-${ATLAS_CORS_ORIGINS:-}}}"
export GEMINI_API_KEY="${GEMINI_API_KEY:-${ATLAS_GEMINI_API_KEY:-}}"

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${QDRANT_URL:?QDRANT_URL is required}"
: "${QDRANT_API_KEY:?QDRANT_API_KEY is required}"
: "${FRONTEND_URL:?FRONTEND_URL is required}"
: "${GEMINI_API_KEY:?GEMINI_API_KEY is required}"
: "${PORT:?PORT is required}"

alembic upgrade head
exec python3 -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --workers "${WEB_CONCURRENCY:-1}" \
  --proxy-headers \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
