# Technical decisions

## 2026-10-22 — M0/M1 vertical slice

- Use FastAPI/Pydantic, SQLAlchemy 2, Alembic, PostgreSQL, Celery/Redis, local private volume, and React/TypeScript/Vite as specified by the implementation plan. Versions are pinned in the backend requirements and frontend lockfile.
- Keep reconciliation and approval in application services. Extraction providers return candidate data only; the fixture adapter is explicitly labeled and SHA-bound.
- Scope all data access by organization from a signed, short-lived access token. Add composite organization foreign keys where records reference each other.
- Use PostgreSQL row locks and unique constraints for immutable versions, idempotency and approval uniqueness. Reconciliation snapshots are append-only.
- Use `NUMERIC(20,6)` and decimal strings in JSON. The browser formats money but does not calculate reconciliation outcomes.
- Store uploaded bytes in a private named volume and enqueue a database job plus outbox event in one transaction. A dispatcher publishes job IDs to Celery; workers claim using leases and monotonically increasing fencing tokens.
- M0/M1 scope implements three synthetic examples only (valid, quantity mismatch, scan). The 12-document dataset expansion and evaluation report belong to M3 as described by the plan.
- At the initial M0/M1 slice, no Bedrock/Foundry/MCP/RAG/multi-agent extensions were implemented. M2 later added the Bedrock adapter; live AI verification remains NOT_RUN.

## 2026-10-22 - Frontend API routing and idempotency claim detection

- The browser uses the same-origin `/api/v1` base. Vite proxies `/api` to `IDP_API_PROXY_TARGET`; Compose connects the frontend to the private network for `http://api:8000` and to the default bridge network so Docker can publish the UI port to localhost. Host-side API port access is not required for browser requests.
- PostgreSQL idempotency reservations use `INSERT ... ON CONFLICT DO NOTHING RETURNING id`. A returned row identifies a new claim; no returned row means an existing scoped key must be checked for payload equality and replayed. Do not use driver `rowcount` to distinguish these cases.
- The local seed command synchronizes existing demo account password hashes with the current `DEMO_PASSWORD`; only the configured demo users are affected.

## 2026-10-26 — M2 Bedrock provider and provenance

- Use boto3 directly with the `bedrock-runtime` Converse operation. The workspace SDK check found boto3 1.43.118 and a Botocore service model exposing Converse; no higher-level framework adapter is needed for this single synchronous worker call.
- Keep `AWS_REGION` and `BEDROCK_MODEL_ID` explicit environment settings. Both are unset in this workspace and live calls are disabled, so no account-specific region/model is selected or probed. `amazon.nova-lite-v1:0` is a candidate where AWS lists it as available; choose a regional or inference-profile ID only after checking the target account's model access and data-routing requirements.
- Converse is used for text and image input with client-side tool calling. The tool is only an extraction output format; it cannot approve invoices or invoke application actions. The adapter sends a small tool schema and always validates returned data with `InvoiceData` and evidence-page checks. Do not describe this as Bedrock native structured-output mode.
- Route usable native PDF text as page-marked text; render scanned/low-text PDF pages and image uploads as bounded JPEGs. Do not default to Textract for Vietnamese invoices. Image evidence remains page-referenced until a reviewer checks it; native-text quotes are verified against their cited page.
- Keep provider raw output in private storage and record model, prompt version, route, tool strategy, token usage, latency, physical-call count, and typed failure code. Never log full prompts or uploaded contents. Provider invocation accounting is capped at three per extraction run.
- Use an application read timeout of 180 seconds and total job deadline of 600 seconds by default, both configurable. SDK retries are disabled so physical invocations are counted by the database; durable retry uses jitter and respects `Retry-After` only if another attempt can start before the job deadline. AWS documents a larger maximum inference timeout for Nova; the app intentionally applies its shorter processing budget.
- Keep the SHA-bound fixture provider independent of AWS credentials and calls. Fixture results remain synthetic; no accuracy claim is derived from them.

AWS documentation checked 2026-10-26:

- [Boto3 Bedrock Runtime Converse](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-runtime/client/converse.html)
- [Amazon Nova Lite model card, APIs, features, and regional availability](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-lite.html)
- [Amazon Nova tool input schema definition](https://docs.aws.amazon.com/nova/latest/userguide/tool-use-definition.html)
- [Amazon Nova inference timeout guidance](https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-nova.html)

## 2026-10-26 — M4 graph delivery, review pause, and restart safety

- Use the PostgreSQL LangGraph saver as the durable workflow checkpoint store. Keep checkpoint payloads limited to internal record IDs and small workflow state; private document bytes and provider raw output stay in their existing storage/services.
- Review remains a domain-service transaction. The decision ID in the resume outbox is only a reference to a persisted reviewer decision; graph state or provider output cannot approve an invoice. Before resume/finalize, verify the job's organization and invoice, current version, reconciliation version/hash, decision, and the job's saved workflow thread.
- Use a PostgreSQL session advisory lock keyed by the internal job ID to serialize at-least-once graph deliveries. A process exit closes the session and releases the lock; a duplicate delivery that overlaps a live worker is a no-op. The lock is held only for the graph execution and does not hold a domain transaction open.
- Route both initial invoice processing and review resume tasks to the worker's `invoice` queue. Celery uses late acknowledgement and worker-loss rejection so a process failure can redeliver work; graph checkpointing and domain-service idempotency handle retries.
- A review interrupt returns from the worker; `WAITING_REVIEW` is not a leased worker state. The reviewer transaction's outbox event is the durable resume trigger. If the immutable invoice or reconciliation snapshot changes while a graph is suspended, rotate to a new server-generated thread and reload authoritative state before using the saved decision.
- Compose health checks cover API database readiness, dispatcher loop progress, Celery responsiveness, and frontend HTTP availability. `scripts/smoke.py` checks API liveness/readiness and frontend HTML. These establish local Compose readiness only; they do not claim a hosted deployment.

## 2026-10-27 — M5a read-only MCP

- Pin the official `mcp` Python SDK at 2.3.0, using its `MCPServer`, Streamable HTTP ASGI transport, and independent `Client`. The server is mounted at `/api/v1/mcp` and runs its session manager from the FastAPI lifespan. SDK v2.3.0 is the version verified against this service; retain the compatible `starlette==0.46.2` and `sse-starlette==3.0.3` pins in the lockfile.
- Use a local CLI to issue high-entropy `idpmcp_` bearer tokens. Persist only the SHA-256 hash, organization, read scopes, creation/expiry, and revocation time. Tokens expire after 30 days by default and cannot be issued for more than 365 days. The CLI prints a token once; revocation uses its UUID. Do not put token plaintext in prompts, source control, or application logs.
- MCP tools are exactly `list_invoices`, `get_invoice_summary`, `get_reconciliation`, and `get_purchase_order`. Each tool checks its read scope and derives `org_id` only from the authenticated token. REST and MCP share the organization-scoped invoice and PO read services; MCP does not create a human reviewer identity.
- MCP returns bounded, whitelisted JSON (up to 100 rows/lines), never file bytes, storage references, preview URLs, or raw extraction text. Extracted/document values are marked and treated as untrusted data. No approve, reject, payment, or other mutation tool is registered. REST role checks and reviewer approval invariants remain in their existing services.
- Streamable HTTP bearer auth is an application middleware because these CLI-issued service tokens are not OAuth access tokens. Do not advertise fabricated OAuth issuer/discovery metadata. The frontend proxy provides the local demo URL; the API remains on Compose's private network.

Official SDK references checked for the pinned implementation:

- [Python SDK releases](https://github.com/modelcontextprotocol/python-sdk/releases)
- [Official ASGI mounting and lifespan guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/run/asgi.md)
- [Official Streamable HTTP client transport guide](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/client/transports.md)

## 2026-10-27 — M5 Foundry extraction adapter

- Use Microsoft's supported OpenAI-compatible Foundry Model Inference API at the resource's `/openai/v1/` endpoint, with the configured deployment name passed in `model`. Do not use the Foundry project endpoint or retired `azure-ai-inference` beta SDK. The adapter targets chat completions because the shared provider contract needs both text and image input, one bounded request, and JSON Schema output.
- Pin the official `openai==3.39.0` Python SDK and `azure-identity==1.27.0`. The current OpenAI SDK uses HTTPX2; keep the existing pinned `httpx2==2.16.0` rather than injecting the legacy `httpx` transport. SDK retries are set to zero so each physical call is reserved and accounted in `provider_calls`.
- Require a deployment that supports Chat Completions, image input, and Structured Outputs. Send strict JSON Schema output with all object properties required and nullable fields for unknown values; always apply the shared Pydantic invoice validation and page evidence checks. The model may return extraction data only; it has no action tools or authority over review/approval.
- Configure only the HTTPS inference endpoint ending in `/openai/v1/` and deployment name. Use `FOUNDRY_API_KEY` only for local secret configuration when required; otherwise use `DefaultAzureCredential` so hosted environments can use managed identity. The token scope follows the documented endpoint family: `cognitiveservices.azure.com` for `*.services.ai.azure.com`, `ai.azure.com` for `*.openai.azure.com`.
- Keep connect/read timeouts, output limit, and job deadline bounded. Persist deployment, `native_schema` strategy, physical call latency/status/failure, and prompt/completion token usage in the existing provider accounting path. `provider_calls.provider` is a free-text field with no provider-specific CHECK; no schema migration is needed.
- No Foundry account, deployment, endpoint, or credential is configured in this workspace. Do not probe or invent a deployment/model/region. Live validation remains **LIVE_PENDING**; fixture regression remains offline and provider-independent.

Microsoft/PyPI references checked 2026-10-27:

- [Foundry SDK and endpoint selection](https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/sdk-overview)
- [Foundry deployment inference endpoint and Entra scope](https://learn.microsoft.com/en-us/azure/foundry/how-to/deploy-models-managed)
- [Foundry structured outputs and schema limits](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/structured-outputs)
- [Foundry Chat Completions request reference](https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/chat)
- [OpenAI Python SDK release 3.39.0](https://pypi.org/project/openai/3.39.0/)
- [Azure Identity](https://pypi.org/project/azure-identity/)
- [OpenAI Python SDK HTTPX2 migration](https://github.com/openai/openai-python/blob/main/httpx2.md)

## 2026-10-27 — M5c read-only invoice copilot

- Use the existing pinned official OpenAI Python SDK through the configured Foundry `/openai/v1/` endpoint for client-side Chat Completions function calling. Do not enable the feature by default or invent a fixture/model answer. A Foundry deployment must support function calling; the local fixture path remains available without cloud credentials.
- Register only five server-owned tools: invoice summary, current reconciliation, the invoice's already-linked PO, bounded page evidence, and policy search. No invoice/org IDs are accepted as model tool arguments; server execution always uses the authenticated actor's organization and the invoice route ID. Every tool uses the shared invoice, PO, and policy read services.
- Validate tool names/arguments in application code even when the model emits schema-shaped arguments. Preserve the model's tool call ID and return the corresponding `tool_call_id` result before another completion. A malformed ID/envelope fails closed. Model content, history, extracted fields, evidence, PO content, and policy text are all untrusted; they cannot authorize tools or reviewer decisions.
- Pin each turn to the initial invoice version/hash, current reconciliation ID/hash, and workflow status. Recheck that binding before every tool read and stop if it changes, so one answer cannot mix immutable snapshots. Keep the model tool set read-only and bounded to four completion rounds, six backend tool executions, and a 45-second wall-clock deadline by default.
- Return an explicit metadata trace (provider, rounds, call ID/name/status/timing, and actual available token counts) for review/debugging. Do not persist user questions, assistant text, tool arguments/results, or chain-of-thought. This is stateless and adds no migration or conversation table.
- The environment has no Foundry endpoint/deployment/credential and live calls are disabled. Contract tests use test doubles only; do not call them live tool-call evidence. Keep actual live verification `LIVE_PENDING` until an authorized configured deployment produces a real trace.

Official function-calling references checked 2026-10-27:

- [Microsoft Foundry function calling](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/function-calling)
- [Official OpenAI Python Chat Completions SDK reference](https://github.com/openai/openai-python/blob/main/src/openai/resources/chat/completions/completions.py)

## 2026-10-27 - M5b synthetic policy retrieval

- Ship a small bundled corpus of seven explicitly synthetic Vietnamese guidance sources. Each organization receives its own copies during the idempotent demo seed. The corpus is not legal, tax, accounting, or real company policy.
- Store each source version and page-level passage in PostgreSQL with organization IDs, source and passage SHA-256 hashes, section, and page. Composite tenant foreign keys bind passages to sources in the same organization. Append-only database triggers prevent edits or deletion; a changed source must be ingested as the next version. Retrieval exposes only the latest version of each source and keeps prior versions available for audit.
- Use PostgreSQL full-text search with the simple configuration and a GIN index. Source and query text are accent-folded consistently for Vietnamese matching while citations return the exact stored source text. Query tokens are normalized and bound as SQL parameters. A minimum token-overlap threshold produces an explicit insufficient-evidence response instead of a weak match.
- Keep this milestone offline and deterministic. No embedding model, vector extension, external API call, or LLM-generated paraphrase is used because this workspace has no configured embedding deployment. The response is extractive: it returns exact passages and their title, version, section, page, source hash, and passage hash. This verifies source/version citations and abstention without pretending to perform semantic accuracy evaluation.
- The REST service derives organization only from the authenticated membership token. The query body has no organization field; questions are bounded and are not persisted or logged. Retrieved text is display-only guidance and cannot change reconciliation rules, permissions, or reviewer decisions.

PostgreSQL references checked 2026-10-27:

- [PostgreSQL 17 text search functions and operators](https://www.postgresql.org/docs/17/functions-textsearch.html)
- [PostgreSQL 17 text search controls and ranking](https://www.postgresql.org/docs/17/textsearch-controls.html)
- [PostgreSQL 17 GIN indexes](https://www.postgresql.org/docs/17/gin.html)

## 2026-10-28 — M6 isolated copilot experiment

- Keep the production copilot single-agent. The optional M6 comparison is a local experiment runner and adds no REST route, workflow state, approval behavior, or write-capable tool.
- Compare the same question and immutable invoice/reconciliation snapshot with the M5c single-agent baseline and three sequential roles: document reader, deterministic-reconciliation explainer, and final explanation/citation checker. Use the existing read services and the same signed membership principal for both arms.
- Reject fixture-mode invoices before any provider invocation. Synthetic fixtures remain plumbing/rule-regression data, not model-accuracy evidence. The current workspace has no eligible human-reviewed nonfixture QA corpus, so a live quality comparison is not claimed.
- Pin each arm to the same Foundry deployment, temperature, per-completion output limit, and wall-clock budget. Bound the multi-agent arm to three completions and four existing read calls; keep the baseline at the M5c limits. Do not retry evaluation runs automatically.
- Validate each role's strict JSON schema and source IDs against the current evidence/reconciliation/PO source catalog. This checks source existence only, not whether a citation entails a claim. Keep answer correctness and citation entailment `NOT_MEASURED` until human review.
- Keep the aggregate report metadata-only. By default do not persist questions, invoice IDs, document/evidence data, tool results, generated notes, or final answers. Human scoring may explicitly opt into a separate local-only review bundle under ignored `var/`; never commit or share it without authorization. Cost stays null unless the operator supplies explicit token rates; never infer deployment model family or pricing from an alias.
- Require both `--live` and `--confirm-cloud-data` before sending eligible nonfixture invoice data to the configured Foundry deployment. The no-network preflight records blockers and the live state without querying credentials or contacting the provider.

## 2026-10-26 — Make migration 0002 tolerate baseline CHECK names

- The same `jobs.status` CHECK has appeared as both `job_status` and `ck_jobs_job_status`, depending on which version of SQLAlchemy's naming convention created the M0/M1 baseline. A fixed `op.f("job_status")` therefore failed on databases that already had the prefixed physical name.
- Migration 0002 now inspects the existing CHECK definition, selects the single legacy jobs-status constraint by its predicate, and preserves its physical name when replacing it. Downgrade uses the same logic. Do not edit revision 0001 to rename an already-created schema.
- PostgreSQL verification covered both physical names, upgrade and downgrade on an isolated tmpfs database. The user's database then upgraded from 0001 to 0002 with aggregate org/document/invoice/job counts unchanged and all `workflow_thread_id` values populated.

## 2026-11-04 — Locale-aware normalization and provider provenance

- Keep provider candidate amounts and dates as raw strings until application normalization. For `vi-VN`, accept only the explicit dot/space grouping and comma-decimal forms; malformed grouping, ambiguous decimal notation, unsupported locale, and values outside `NUMERIC(20,6)` become a null normalized value plus a review issue. Do not infer a replacement value.
- Normalize ISO dates directly. Interpret a non-ISO `vi-VN` date as day-first only when its day is greater than 12 and therefore has one valid reading; otherwise retain the raw string and require review. Invalid calendar dates also require review.
- Persist raw and normalized values separately in evidence rows. Missing remains null and is not converted to zero. An unresolved normalization issue is attached to the reconciliation snapshot as a blocking review issue. Missing evidence for a non-null extracted value is treated as unverified provenance; amount, tax, PO matching, and reviewer authorization rules remain unchanged. `tax_total` remains invoice-level.
- Treat provider page, bounding region, and confidence as optional. Leave each absent value null; validate and pass through bounding regions/confidence only when the provider adapter actually supplies them. No default page, bbox, confidence, or quote is synthesized. Provider/live behavior was not tested in this workspace.
- Enforce the application-side `NUMERIC(20,6)` boundary as at most 14 integer digits and 6 fractional digits, without rounding, and reject non-finite Decimal values.
- Migration `0005_provenance` makes `page_no` nullable and adds nullable confidence and normalization-issue columns/checks. It does not alter tax schema. It preserves existing evidence and does not rewrite invoice versions or reconciliation snapshots.

## 2026-11-04 — Azure Document Intelligence adapter

- Use the official `azure-ai-documentintelligence` Python SDK pinned at 1.0.2, REST API `2024-11-30`, and `prebuilt-invoice`. Microsoft's SDK reference documents the client operation and stable API version; the invoice model supports PDF/JPEG/PNG and Vietnamese (`vi`). See the [SDK overview](https://learn.microsoft.com/en-us/python/api/overview/azure/ai-documentintelligence-readme?view=azure-python), [SDK release history](https://github.com/Azure/azure-sdk-for-python/blob/main/sdk/documentintelligence/azure-ai-documentintelligence/CHANGELOG.md), and [prebuilt model language/input support](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/language-support/prebuilt?view=doc-intel-4.0.0).
- This request explicitly prioritizes Azure for the current invoice MVP, superseding the original plan's Bedrock P0/M2 priority and its proposed Azure Read/Layout P1 experiment. Use the invoice-specific prebuilt model for the existing invoice schema. Keep Bedrock and Foundry selectable and unchanged. The service region is determined by the configured Azure resource endpoint; no endpoint or region was selected or probed in this workspace.
- Azure is selected only by `EXTRACTION_PROVIDER=azure_document_intelligence` with `ALLOW_LIVE_MODEL_CALLS=true`; `fixture` remains the offline default. The adapter receives the original file bytes only for that explicit provider and applies the existing 10-page/15 MiB app limits. SDK retries are disabled. Provider errors remain terminal/typed for the selected provider; there is no provider fallback.
- Read the field `content` returned by the invoice model as the raw value and pass it through the shared locale normalizer. Store only returned provenance: page number is taken from a provider bounding region when present; bounding region and confidence remain nullable. If Azure omits a page, the existing extraction service marks `MISSING_PROVENANCE` and existing reconciliation/approval guards require reviewer attestation.
- Store no full Azure response payload. The service response does not provide token usage in this adapter contract, so token counts and cost remain unknown. Record measured request latency, actual call status, and the app's observed provider-call count only.
- No database schema, tax model, reconciliation rule, immutable-version, or approval change is needed. No migration is added. Offline fake-client contract tests establish adapter plumbing only; Azure live verification remains **LIVE_PENDING** and no provider result or accuracy report is claimed.

## 2026-11-04 — Post-0005 source-page provenance guard

- Keep migration `0005_provenance` unchanged. Add `0006_page_provenance`: a stored generated `provenance_state` reports `MISSING_PROVENANCE` whenever `page_no` is null, and a strict `page_no`/`verification_state` CHECK rejects new null-page evidence unless its stored state is `MISSING_PROVENANCE`.
- Add the CHECK as `NOT VALID` when upgrading a legacy schema. `extraction_evidence` is append-only, so migration does not rewrite old evidence rows. PostgreSQL still enforces the CHECK for new writes; the generated state exposes old null-page rows as missing provenance without changing their raw values, normalized values, versions, or snapshots.
- Reconciliation independently checks every populated canonical field against the current document's page count. Missing/out-of-range pages create field-level blocking issues, set the snapshot to `BLOCKED`, and keep the invoice in `REVIEW_REQUIRED`; the existing decision service rejects approval for that snapshot.
- A reviewer can add a page only after explicitly entering the page number and confirming the source. The service validates the page against document length and writes a new immutable evidence row/version. It carries raw and normalized values forward only for an unchanged value; manual evidence has no inferred raw text, bbox, or confidence. Changed populated values require an explicit manual source page.
- Valid provider pages and available raw/normalized metadata remain attached to new immutable versions. Missing values remain distinct from zero. No tax schema or reconciliation/approval rule was changed.
- Verification used only the isolated PostgreSQL test container. A synthetic legacy null-page row retained its stored `UNVERIFIED` state while generated `provenance_state` returned `MISSING_PROVENANCE`; the migration did not update the append-only evidence row. No application database, live provider, or external API was used. Frontend typecheck/build remains unverified because local binaries are absent; see `docs/progress.md`.

## 2026-11-04 — Offline Azure evaluation route

- Add Azure Document Intelligence as an explicit AI-corpus evaluator provider through the existing extraction request/result contract and `_make_request` path. Keep fixture as the default; never fall back to fixture output for an Azure failure.
- A live Azure evaluation requires `--live`, `ALLOW_LIVE_MODEL_CALLS=true`, a valid HTTPS resource endpoint, and the locally installed SDK matching the lock pin. Preflight checks local package metadata and endpoint syntax only; it does not construct an SDK client, resolve credentials, or contact Azure.
- Record the installed and required SDK version, REST API version, model ID, sample/attempt counts, and successful-call latency. `prebuilt-invoice` does not expose an immutable service build identifier or token/cost usage through this contract; report these as null unless the provider later returns them. Do not estimate zero cost or turn fake-client output into accuracy.
- Preserve the dataset manifest, labels, split boundaries and existing reports. The evaluator change requires no database/schema change and does not alter reconciliation, evidence, versioning, or reviewer approval behavior. Azure live evaluation remains `LIVE_PENDING`.

