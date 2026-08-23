# Atlas Production Scaling Design

## Objective

Scale Atlas to 1,000 active users, 100 mixed API requests per second, and 25 concurrent document ingestions without losing project isolation, accepted uploads, or evidence provenance. Keep the existing FastAPI application as a modular monolith, make API processes stateless, and move heavy or shared work to independently scalable infrastructure.

## Capacity Contract

The first production scaling release must satisfy this load profile:

- 1,000 simulated active users.
- 100 mixed API requests per second across metadata, status, dashboard, retrieval, and Copilot routes.
- 25 document ingestions executing concurrently across all worker replicas.
- A 30-minute steady-state load run with less than 1% unexpected API errors.
- Metadata and ingestion-status p95 latency below 300 ms.
- Retrieval p95 latency below 1.5 seconds before external LLM generation.
- Upload acceptance below one second after the object transfer completes.
- No unbounded API-process memory growth or PostgreSQL connection growth.

External LLM latency and availability are measured separately because Atlas cannot control provider response time. The 100-request-per-second target is a mixed API workload, not 100 simultaneous LLM generations per second.

## Current Scaling Constraints

The current implementation has four immediate horizontal-scaling blockers:

- `POST /projects/{project_id}/documents` reads an entire upload into API memory, writes it to the instance filesystem, and executes ingestion before responding.
- Lexical retrieval scrolls every matching Qdrant payload into the API process and calculates BM25 in Python, making query cost grow linearly with project corpus size.
- Original documents and NetworkX graph snapshots live on instance-local filesystems, so replicas do not share state.
- Database pools, request limits, expensive-operation concurrency, and cache behavior are not explicitly bounded.

The existing PostgreSQL records, Qdrant project filters, ingestion status endpoint, deterministic domain engines, and typed frontend client remain the foundation of the scaled design.

## Chosen Architecture

Atlas remains one deployable codebase with two runtime roles:

1. Stateless FastAPI replicas handle validation, metadata transactions, retrieval orchestration, and response generation.
2. Ingestion worker replicas execute extraction, OCR, chunking, embedding, vector indexing, entity synchronization, and graph snapshot updates.

Shared production dependencies are:

- PostgreSQL as the source of truth for projects, documents, jobs, workflow records, leases, and the transactional outbox.
- Redis for queue transport, bounded caches, rate limits, distributed locks, and the global ingestion semaphore.
- S3-compatible object storage for original documents and graph snapshots.
- Qdrant for server-side dense and sparse retrieval.

Redis is never the only durable record of accepted work. Losing Redis may reduce throughput or temporarily delay work, but it cannot erase an accepted upload or its ingestion state.

## Component Boundaries

### Object storage

Create an `ObjectStore` protocol with streaming upload/download, existence, and delete operations. Provide:

- `S3ObjectStore` for production, configured with endpoint, region, bucket, access key, secret key, and path-style addressing when required by MinIO or another compatible provider.
- `FileObjectStore` for local development and focused tests.

Original objects use `projects/{project_id}/documents/{document_id}/original/{filename}`. Graph snapshots use `projects/{project_id}/graph/snapshot.json`. Database `storage_path` values become backend-neutral object keys rather than host filesystem paths.

Uploads use the existing `UploadFile` spooled file, calculate SHA-256 incrementally, validate the size limit while streaming, rewind, and transfer without loading the complete file into Python bytes. Synchronous S3 SDK work runs through `asyncio.to_thread` so it cannot block the FastAPI event loop.

### Job queue and worker

The API transaction inserts or updates three durable records atomically:

- `Document` in `queued` state.
- `IngestionJob` in `queued` state.
- `OutboxEvent` containing the ingestion job identifier and an idempotency key.

A dispatcher publishes undispatched outbox events to a Redis Stream consumer group and marks the event dispatched only after Redis acknowledges it. A periodic reconciliation pass republishes old undispatched events. Publishing the same job more than once is safe.

A worker claims a job with a conditional PostgreSQL update. A claim sets `status=processing`, a unique lease owner, a lease expiry, a heartbeat timestamp, and increments `attempt_count`. Completed jobs return without work; an unexpired lease cannot be stolen; expired leases can be reclaimed. Workers acknowledge Redis messages only after the final database state is committed.

The worker runtime uses a Redis lease-backed semaphore with 25 total permits across replicas. Semaphore permits have expirations and are renewed with the job heartbeat so a crashed worker cannot permanently consume capacity. CPU-heavy extraction and OCR execute outside an async event loop in worker processes.

### Graph snapshots

Refactor `GraphStore` behind a protocol. The S3 implementation acquires a project-scoped Redis lock, reads the latest snapshot, applies the document update, writes a versioned temporary object, and promotes it to the canonical key. The filesystem implementation remains for local development. PostgreSQL domain entities remain authoritative; a graph snapshot failure is retryable and cannot cause a logically incomplete job to be reported as completed.

### Hybrid retrieval

Replace Python-side `_filtered_payloads` plus `_bm25_rank` query execution with Qdrant named vectors:

- A named dense vector stores the existing semantic document embedding.
- A named sparse vector stores deterministic lexical token identifiers and term frequencies.
- Qdrant's sparse index applies inverse-document-frequency weighting.
- Dense and sparse prefetches use the same project, document, equipment, section, revision, and index-version filters.
- Qdrant performs reciprocal-rank fusion and returns only the configured candidate limit.

Parent expansion may still retrieve a bounded set of chunks for one selected parent. No normal retrieval request may scroll an entire project corpus into API memory. The change uses a new versioned collection and index version so the current collection remains available during reindex and rollback.

### Cache and invalidation

Redis caches only responses that are safe to reproduce without claiming stale live generation. The initial cache scope is:

- Project and document metadata lists.
- Ingestion status.
- Dashboard aggregates.
- Deterministic retrieval candidates before LLM generation.

LLM answers are not cached as live responses in this release.

Every cache key includes the environment, project ID, endpoint/schema version, normalized request payload, configured index version, and a project generation number. Successful ingestion and project mutations atomically increment the generation number. Old keys expire naturally and are never searched or deleted with wildcard scans. Cache failures bypass the cache and record a metric.

### Rate limits and backpressure

Implement Redis token buckets with separate configurable budgets for cheap reads, retrieval/Copilot, and uploads. The identity key uses an authenticated actor when one becomes available; until application authentication exists, it uses project ID plus trusted proxy client address. A global safety bucket protects the service independently of per-client buckets.

The default aggregate limits allow the 100-request-per-second capacity target. Exhausted buckets return `429` with `Retry-After`. Bounded semaphores protect in-process reranking and external LLM calls; overload is rejected or queued for a short configured interval rather than consuming unbounded memory.

### Database connections and pagination

Expose PostgreSQL pool size, overflow, acquisition timeout, recycle interval, and statement timeout as settings. Production uses PgBouncer-compatible transaction pooling. The deployment must calculate the sum of API and worker pool maxima and keep it below the provider connection allowance with operational headroom.

Collection endpoints accept `limit` and opaque cursor parameters, enforce a maximum page size of 100, and order by a stable `(created_at, id)` tuple. To preserve current clients, the JSON body remains a list and the next cursor is returned in an `X-Next-Cursor` response header. Missing pagination parameters use a bounded default rather than returning every row.

## API and State Transitions

`POST /projects/{project_id}/documents` changes from synchronous `201` completion to asynchronous `202 Accepted`. It returns the existing `UploadResponse` shape with both document and ingestion objects in `queued` state. The frontend polls `GET /projects/{project_id}/documents/{document_id}/ingestion` with bounded exponential backoff and stops on `completed` or `failed`.

The legal ingestion states are:

`queued -> processing -> completed`

`queued -> processing -> retrying -> processing`

`queued|processing|retrying -> failed`

Retrying jobs record a public-safe error code, an operator-facing error message, and `available_at`. The status API never exposes credentials, provider response bodies, stack traces, or source document contents in error fields.

Duplicate content remains rejected per project. An optional `Idempotency-Key` header additionally allows clients to retry an interrupted upload request without creating a second document. Idempotency records are scoped to project, route, and request fingerprint and expire after 24 hours.

## Consistency and Failure Handling

Failures are classified explicitly:

- Retryable: Redis timeouts, S3 transient errors, database serialization or connection failures, Qdrant availability errors, and worker termination.
- Terminal: unsupported file type, size violation, corrupt input that deterministic parsing rejects, invalid project scope, and permanently incompatible index configuration.

Retryable jobs use exponential backoff with jitter and a maximum of five processing attempts. Jobs that exhaust attempts enter `failed` state and their queue message is copied to a dead-letter stream for operations inspection. PostgreSQL retains the canonical failure record.

Before indexing, a worker deletes or replaces Qdrant points for the same project and document. Stable chunk identifiers and conditional job claims make duplicate delivery idempotent. A job is marked completed only after Qdrant indexing, SQL entity synchronization, graph snapshot update, and SQL metadata commit succeed.

Dependency behavior is:

- PostgreSQL unavailable: readiness fails and state-changing requests fail with `503`.
- S3 unavailable: new uploads fail with `503`; existing metadata reads may continue.
- Redis unavailable: readiness reports queue/cache/rate-limit degradation, caches are bypassed, accepted jobs remain in the outbox, and a conservative process-local limiter protects each API replica.
- Qdrant unavailable: retrieval and ingestion indexing fail with `503` or retry; metadata APIs may continue.
- LLM unavailable: existing evidence-only fallback behavior remains; provider failure never changes durable project state.

## Configuration

Add settings for:

- Runtime role: API, worker, dispatcher, or all-in-one local development.
- Redis URL, stream/group names, cache TTLs, rate budgets, lock TTL, and semaphore size fixed to a production default of 25.
- S3 endpoint, region, bucket, credentials, secure transport, and path-style mode.
- Database pool bounds and timeouts.
- Worker lease duration, heartbeat interval, maximum attempts, backoff bounds, and reconciliation interval.
- Sparse vector name, dense vector name, new collection name, and index version.
- Pagination defaults and maximum of 100.
- Reranker and LLM concurrency bounds.

Secrets are accepted through environment variables and never serialized in logs or returned by readiness endpoints. Local Compose supplies Redis and MinIO with non-production credentials; production configuration must not inherit those defaults.

## Observability and Operations

Every request receives or propagates a correlation ID. Worker logs include correlation ID, project ID, document ID, and job ID but never extracted content. Logging uses structured JSON in production and concise text locally.

Expose Prometheus-format metrics for:

- Request count, latency, response status, and in-flight requests by route template.
- Database pool checkout latency and pool usage.
- Redis cache hits, misses, bypasses, rate-limit rejections, and lock contention.
- Queue depth, oldest queued age, active permits, processing duration, retries, lease recoveries, and dead-letter count.
- S3 and Qdrant operation latency and failures.
- Retrieval dense/sparse candidate counts, rerank latency, and total pre-generation latency.

`/health` remains a process liveness check. `/ready` checks the dependencies required by the configured runtime role and reports only `ok`, `degraded`, or `error` per component. A worker command exposes the same checks for deployment probes.

Alert recommendations are queue oldest-age above five minutes, any dead-letter increment, sustained API error rate above 1%, metadata p95 above 300 ms, retrieval p95 above 1.5 seconds, database pool saturation above 80%, and ingestion semaphore saturation lasting more than ten minutes.

## Deployment Topology

Production deploys API, dispatcher, and worker process types from the same image. Database migrations run as a one-off release command, never concurrently in every API replica. API replicas may scale horizontally behind the existing proxy. Worker replicas may scale independently, while the Redis semaphore keeps global ingestion concurrency at 25.

The image contains only application code and required parsing libraries. Uploaded files and graph snapshots never rely on the container filesystem after a request or job exits. Graceful shutdown stops accepting new work, allows a bounded drain period, releases leases when possible, and leaves unfinished jobs recoverable after lease expiry.

## Testing Strategy

All behavior changes follow test-driven development.

### Unit tests

Cover streaming size/hash validation, deterministic object keys, object-store interfaces, outbox serialization, claim predicates, lease renewal and expiry, retry classification, semaphore ownership, sparse vectors, cache keys, generation invalidation, token buckets, cursor encoding/decoding, and configuration validation.

### Integration tests

A Docker Compose test profile runs PostgreSQL, Redis, MinIO, and Qdrant. Integration tests verify:

- Upload returns `202`, dispatch occurs, and a worker reaches `completed`.
- Redis publication failure is repaired from the outbox.
- Duplicate messages execute one logical ingestion.
- Worker termination is repaired after lease expiry.
- S3 objects and Qdrant points remain project-isolated.
- Dense and sparse filtered retrieval returns expected citations without a full collection scroll.
- Cache invalidation prevents stale project responses.
- Rate limits are shared across API replicas.
- Graph snapshot updates do not lose concurrent document changes.

### Frontend tests

Cover queued, processing, retrying, failed, and completed displays; polling backoff; polling termination; `429` retry messaging; and cursor-based incremental loading.

### Fault and load tests

Fault tests interrupt Redis, S3, Qdrant, and workers at each durable boundary. No accepted upload may disappear, no terminal job may be reported completed, and retry recovery must not create duplicate project entities or chunks.

A checked-in Locust scenario exercises 1,000 active users, 100 mixed requests per second, and 25 concurrent ingestions for 30 minutes. The report records latency by route class, unexpected error rate, queue age, database connections, memory, retry recovery, and cross-project isolation checks. The capacity contract is a release gate, not an unsupported README claim.

## Rollout and Rollback

Roll out in this order:

1. Add configuration, storage/queue abstractions, schema migrations, Redis, and S3 while retaining local adapters.
2. Deploy the dispatcher and workers with asynchronous ingestion disabled at the API feature flag.
3. Reindex into the new named-vector Qdrant collection and validate retrieval parity.
4. Enable asynchronous ingestion for internal projects, then all projects.
5. Enable caching and rate limiting in observe-only mode, then enforcement.
6. Scale API and worker replicas and run the capacity test.

Rollback disables asynchronous ingestion, cache enforcement, and the new collection feature flags independently. The old Qdrant collection is retained through the validation window. PostgreSQL migrations are additive until the scaled path has passed production verification; object keys and durable job records are not deleted during rollback.

## Non-Goals

- No decomposition into independently versioned microservices.
- No Kubernetes requirement; the process types work on Render or another container platform.
- No application authentication or RBAC implementation in this scope. Rate-limit identity upgrades automatically when trusted actor identity becomes available.
- No caching of LLM answers as if they were live responses.
- No replacement of PostgreSQL or Qdrant.
- No claim that the target capacity is met until the checked-in load test report passes the capacity contract.
- No deletion of the existing Qdrant collection or the untracked submission PDF.
