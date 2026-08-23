# Deployment-First Multi-Provider Routing Design

**Date:** 2026-08-23

**Status:** Approved design, pending implementation-plan review

**Repository:** `Prabhav200511/project-Atlas`

**Production branch:** `main`

## Summary

Project Atlas will first restore and verify its existing Render and Netlify deployment. After a verified rollback point exists, Atlas will replace its provider-specific LLM gateway with a thin native multi-provider router. Groq and Google Gemini will be the active providers; Mistral AI, NVIDIA NIM, Cloudflare Workers AI, and OpenRouter will be configured standby providers. Free-tier endpoints may process only public or synthetic data. Confidential data must be rejected unless the selected endpoint has an explicitly approved no-training/no-retention policy.

The work is divided into three independently testable workstreams:

1. Production deployment recovery.
2. Provider-neutral routing and resilience.
3. Public construction-data evaluation.

Each workstream receives its own implementation plan, commit sequence, review gate, deployment gate, and rollback procedure. Deployment recovery is first.

## Current State

Atlas currently has:

- A FastAPI backend deployed by `render.yaml` and `scripts/start_production.sh`.
- A Next.js frontend intended for Netlify.
- PostgreSQL metadata storage and Qdrant vector retrieval.
- A `GeminiGateway` that actually implements Groq-first and Gemini-second behavior.
- Evidence sufficiency, citation validation, and a deterministic evidence-only fallback.
- Publicly documented deployment blockers: the configured Render target did not produce an HTTP response, and the configured Netlify target served the wrong application.

The current deployment must not be described as working until production probes and a browser-based smoke test pass.

## Goals

- Restore the current production deployment before changing model behavior.
- Preserve Atlas's evidence-first workflow and deterministic fallback.
- Support six provider adapters through one internal contract.
- Run Groq and Gemini as active providers, with four ordered standbys.
- Prevent prohibited data from reaching a provider before any network call occurs.
- Fail over only for retryable availability failures.
- Record metadata sufficient to diagnose latency, quota, routing, and reliability without storing prompts or completions.
- Evaluate ingestion, retrieval, answering, privacy, and failover using reproducible public construction documents.
- Keep model identifiers, provider order, privacy approval, and budgets configurable because free-tier availability changes.

## Non-Goals

- Using multiple free tiers to evade a provider's rate limits.
- Sending customer-confidential documents to free-tier endpoints.
- Replacing Atlas's retrieval, evidence gate, or citation verifier with provider output.
- Adding a separate LiteLLM proxy service in the first release.
- Making OpenRouter the sole gateway.
- Implementing IFC ingestion in this effort.
- Migrating the application to AWS before the existing deployment has been repaired and measured.
- Claiming enterprise scale from free provider quotas.

## Selected Providers

| Order | Provider | Role | Rationale | Default data permission |
|---|---|---|---|---|
| 1 | Groq | Active primary | Existing Atlas integration, low latency, OpenAI-compatible request shape | Public and synthetic |
| 2 | Google Gemini | Active secondary | Existing Atlas integration, long context, independent model family | Public and synthetic |
| 3 | Mistral AI | Standby | Direct provider with technical-document-capable models and an OpenAI-style API | Public and synthetic |
| 4 | NVIDIA NIM | Standby | Broad independent model catalog and useful evaluation capacity | Public and synthetic |
| 5 | Cloudflare Workers AI | Standby | Serverless inference and controlled free allocation | Public and synthetic |
| 6 | OpenRouter | Emergency standby | Broad fallback coverage and endpoint-level privacy controls | Public and synthetic; confidential only with enforced approved endpoint policy |

No adapter is considered confidential-approved merely because it exists. Confidential approval is an explicit deployment setting backed by a reviewed provider agreement and endpoint policy.

Provider and model availability will be validated at deployment time. Model IDs are environment configuration, not permanent code defaults. A provider whose configured model fails the capability canary remains disabled.

## Architecture

Atlas will use a thin native router inside the FastAPI process:

```text
Knowledge workflow
  -> ModelRequest
  -> PrivacyPolicy
  -> Capability and health filters
  -> ModelRouter
  -> ProviderAdapter
  -> GenerationResult
  -> Schema validation
  -> Atlas evidence and citation verification
  -> User response
```

This keeps Atlas's current workflow authoritative and avoids adding a separately deployed gateway. Provider-specific request formatting, authentication, and error parsing are confined to adapters.

### Internal Contracts

`ModelRequest` contains:

- `request_id: UUID`
- `project_id: UUID | None`
- `purpose: planning | answering | verification`
- `instructions: str`
- `content: str`
- `json_output: bool`
- `data_classification: public | synthetic | confidential`
- `deadline_ms: int`
- `max_attempts: int`

`GenerationResult` contains:

- Generated text.
- Provider and model identifiers.
- Attempt and fallback position.
- Latency.
- Input and output token counts when supplied by the provider.
- Normalized result category.
- Provider request identifier when supplied.

`ModelProvider` exposes an asynchronous `generate(ModelRequest) -> GenerationResult` operation and declares its capabilities. Provider adapters must not implement routing policy.

### Routing Policy

1. Reject requests that have no privacy-eligible provider before making a network call.
2. Select configured providers in the approved order.
3. Exclude providers that lack the required JSON or context capability.
4. Exclude providers with an open circuit or exhausted configured budget.
5. Attempt Groq first and Gemini second when both are eligible.
6. Consider standbys in the approved order only after retryable failures.
7. Respect the request deadline across the complete fallback chain.
8. Respect provider `Retry-After` responses without exceeding the request deadline.
9. Never hedge the same prompt concurrently across providers in this release.
10. Preserve Atlas's evidence-only fallback when the provider chain is unavailable.

Fallback is allowed for connection failures, timeouts, HTTP `429`, and retryable provider `5xx` responses. Fallback is not allowed for invalid credentials, privacy rejection, invalid application input, or a request that violates the provider contract. Malformed or schema-invalid generation is reported distinctly and does not silently become a trusted answer.

### Circuit Breakers

Circuit state is maintained per provider and model. Consecutive retryable failures open the circuit for a configured cooldown. A bounded half-open probe determines recovery. For the initial single-instance Render deployment, process-local circuit state is sufficient. Distributed circuit state is deferred until Atlas runs multiple backend replicas; at that point it moves to Redis or another shared coordination store.

## Data Classification and Privacy

Every model request must have an explicit classification. The default is not inferred from provider configuration.

- `public`: content obtained from reviewed public sources.
- `synthetic`: Atlas-generated demo or evaluation content containing no customer data.
- `confidential`: customer, project, contractual, commercial, personal, or otherwise non-public content.

Free-tier adapters default to `confidential_approved=false`. Privacy policy runs before adapter selection and before logging provider-attempt metadata. A confidential request with no approved provider returns a safe model-unavailable result and continues through Atlas's deterministic evidence-only behavior where possible.

Prompts, retrieved evidence, and completions are not written to logs or model-invocation audit rows. API keys are stored only in deployment secret configuration. Exceptions must be sanitized before logging.

## Configuration

Configuration covers:

- Router enable flag.
- Active and standby provider order.
- Per-provider API key, base URL, and model ID.
- Provider enable flag.
- Public, synthetic, and confidential permissions.
- Connection and read timeouts.
- Global request deadline and maximum attempts.
- Circuit-breaker threshold and cooldown.
- Daily request and token budgets.
- Emergency provider-disable flag.

Missing standby credentials do not prevent API startup. Missing credentials for both active providers make generation unavailable while preserving deterministic fallback. Optional provider health does not make `/ready` fail; database and Qdrant readiness remain mandatory.

## Metadata Auditing and Observability

Model invocation records contain only:

- Request and project identifiers.
- Purpose and data classification.
- Provider and model.
- Result category and HTTP status class.
- Attempt and fallback position.
- Latency and token counts.
- Circuit state transition.
- Timestamp.

Metrics include request count, success rate, retryable failure rate, fallback rate, policy rejection count, circuit state, latency percentiles, and token consumption by provider/model/purpose. No public endpoint exposes credentials, raw provider errors, prompts, or provider-account details.

## Workstream 1: Production Deployment Recovery

The current baseline is deployed before router code is enabled.

1. Confirm the production GitHub source and exact `main` SHA.
2. Inspect Render service events, build logs, runtime logs, start command, region, and environment configuration.
3. Verify database connectivity, Qdrant connectivity, Alembic head, required secrets, CORS origins, and provider configuration.
4. Correct the backend startup or environment failure.
5. Require `/health`, `/ready`, and `/docs` to return within bounded time.
6. Correct the Netlify repository, branch, base directory, build command, publish output, and `NEXT_PUBLIC_API_URL`.
7. Verify the frontend through the browser, including API connectivity and current truth labels.
8. Create a synthetic production project, ingest a small supported document set, and verify evidence-backed Copilot behavior.
9. Record deployment URL, commit SHA, probe evidence, known limitations, and rollback instructions.

No router behavior changes are combined with this recovery deployment.

## Workstream 2: Provider-Neutral Routing

1. Introduce provider-neutral request, result, capability, and error types.
2. Refactor existing Groq/Gemini behavior behind adapters without changing routing behavior.
3. Add privacy policy and prove prohibited requests result in zero adapter calls.
4. Add deterministic routing, deadlines, normalized errors, circuit breakers, and metadata auditing.
5. Add Mistral, NVIDIA NIM, Cloudflare Workers AI, and OpenRouter adapters independently.
6. Add configuration validation and sanitized startup diagnostics.
7. Deploy with the router disabled.
8. Enable it for synthetic canary traffic.
9. Enable approved public/synthetic traffic after acceptance gates pass.

## Workstream 3: Public Construction-Data Evaluation

The initial corpus uses:

- Official UFGS electrical, switchgear, UPS, commissioning, quality-control, and equipment sections.
- One coherent public SAM.gov construction solicitation package containing supported PDFs and schedule-like tabular data.
- Expert-authored gold questions and expected citations.
- Explicitly labelled derived synthetic RFIs or change orders only when a public source package lacks those records.

A source manifest records URL, agency, retrieval date, public-access status, checksum, document type, redistribution notes, and redaction status. Source files are downloaded reproducibly into an ignored cache. Files are not committed when redistribution terms are unclear. Irrelevant personal contact details are removed from evaluation inputs.

buildingSMART IFC samples are documented as a future corpus and are not included until Atlas supports IFC ingestion.

### Evaluation Gates

- Supported-file ingestion success: at least 95%.
- Recall@12: at least 0.90.
- Correct-document rate: at least 0.80.
- Citation precision: at least 0.95.
- Accepted unsupported claims: zero.
- Insufficient-evidence accuracy: at least 0.90.
- Privacy-policy enforcement: 100%, with zero prohibited outbound calls.
- Injected retryable-failure fallback: 100%.
- Authentication, policy, and invalid-request failures must not trigger fallback.

Evaluation covers extraction, retrieval, answering, revision selection, cross-provider consistency, retryable failures, invalid generations, exhausted provider chains, and privacy enforcement. Unit and CI tests use mocked providers. Live provider calls run only as explicit secret-enabled canaries.

## Rollout and Rollback

1. Recover and verify the baseline deployment.
2. Create a deployment rollback point at the verified SHA.
3. Deploy router code with `enabled=false`.
4. Run unit, integration, full regression, and real-data evaluation gates.
5. Enable synthetic canary traffic.
6. Inject timeout, `429`, `5xx`, malformed-output, and provider-disable scenarios.
7. Enable public/synthetic production traffic.
8. Keep confidential model routing disabled until provider approval is recorded.

Rollback is performed first by disabling the router feature flag. If application rollback is required, deploy the recorded baseline SHA. Database changes must remain backward-compatible until the new deployment has passed its observation window.

## Risks and Mitigations

- **Free tiers change without notice:** model IDs and providers are configuration; capability canaries disable incompatible endpoints.
- **Provider behavior differs:** every adapter runs the same contract and schema suite.
- **Fallback increases latency:** one deadline covers the chain and caps attempts.
- **Fallback leaks restricted data:** privacy eligibility is evaluated before every attempt.
- **Retries increase cost:** budgets, attempt caps, and metadata metrics are mandatory.
- **Router obscures evidence failures:** Atlas's evidence and citation verification remain downstream and authoritative.
- **Production recovery mixes unrelated changes:** baseline deployment recovery is a separate workstream and commit series.
- **Public documents have redistribution constraints:** manifest and downloader are committed; source files are cached rather than committed unless redistribution is clearly allowed.

## Acceptance Criteria

The design is complete when:

- Render and Netlify serve the intended application at recorded URLs.
- Production health, readiness, API, ingestion, and browser smoke tests pass.
- Groq and Gemini operate through the provider-neutral contract.
- All six adapters pass the shared provider contract suite.
- Privacy tests prove zero prohibited outbound calls.
- Retry and fallback semantics match this specification.
- Router-disabled behavior matches the baseline.
- Real-data evaluation meets every stated gate.
- Deployment, rollback, provider configuration, privacy controls, and corpus provenance are documented.

## Source References

- Provider inventory: https://github.com/mnfst/awesome-free-llm-apis
- Groq rate limits: https://console.groq.com/docs/rate-limits
- Groq data controls: https://console.groq.com/docs/your-data
- Gemini pricing and tier data use: https://ai.google.dev/gemini-api/docs/pricing
- Cloudflare Workers AI pricing: https://developers.cloudflare.com/workers-ai/platform/pricing/
- OpenRouter privacy and free-tier limitations: https://openrouter.ai/docs/faq
- OpenRouter zero-data-retention controls: https://openrouter.ai/docs/guides/features/zdr
- UFGS public library: https://www.wbdg.org/dod/ufgs
- SAM.gov public construction package example: https://sam.gov/opp/0f4d4a544d0a44059200f85bd6092427/view
- buildingSMART IFC examples: https://technical.buildingsmart.org/standards/ifc/ifc-examples/
