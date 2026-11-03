# M0–M6 Operations Runbook

This runbook covers the local Docker Compose deployment. It does not describe a public production deployment. The app runs fixture mode without an API key; fixture results stay visibly labeled as synthetic demo data. The fixture provider accepts only exact files from the labeled sample manifest: the UI points users to those samples, and the API rejects other uploads before storing them or creating jobs. Processing an arbitrary invoice requires an explicitly configured live provider.

## Start or update the local stack

Run these commands from the repository root in PowerShell:

```powershell
python scripts/bootstrap.py
docker compose up --build -d --wait
docker compose exec -T api alembic upgrade head
docker compose exec -T api python -m app.workflows.checkpoint_setup
docker compose exec -T api python -m app.seed
python scripts/smoke.py --demo-login
```

`bootstrap.py` creates a local `.env` with generated secrets. Keep it untracked. PostgreSQL migrations and LangGraph checkpoint setup are explicit operations; API replicas do not perform schema changes at startup. `checkpoint_setup` delegates checkpoint schema creation to the installed LangGraph PostgreSQL saver.

Open <http://localhost:5173>. Only the frontend is published to the host; the API, PostgreSQL, and Redis stay on the private Compose network. The API readiness route checks PostgreSQL and Redis. Compose separately monitors the dispatcher heartbeat, Celery worker ping, and frontend HTTP response.

## Health and smoke checks

```powershell
docker compose ps
docker compose exec -T api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health/live').read().decode()); print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready').read().decode())"
python scripts/smoke.py --demo-login
```

Expected API responses are `{"status":"alive"}` and `{"status":"ready","database":"ok","rate_limit_store":"ok"}`. `python scripts/smoke.py --demo-login` checks both endpoints from inside the API container, the frontend HTML page, seeded operator/reviewer login through the frontend proxy, one source-cited policy query, and the authenticated copilot status/budget response (without calling a model). It reads the local `.env` in memory and never prints credentials or tokens. Use `--api-url` only when the API is intentionally reachable at a separate URL; `--frontend-url` overrides the UI target.

Container health states are not a substitute for an end-to-end reviewer walkthrough. Check logs without printing document bytes or credentials:

```powershell
docker compose logs --tail 100 api dispatcher worker frontend
```

## Read-only MCP service tokens (M5a)

The MCP Streamable HTTP endpoint is available through the local frontend proxy at `http://localhost:5173/api/v1/mcp`. The API is not published as a host port. Issue and revoke tokens only from a trusted local development environment:

```powershell
docker compose exec -T api python -m app.mcp.cli issue --org demo-a --name "local read-only demo" --scope invoices:list --scope invoices:summary --scope invoices:reconciliation --scope purchase_orders:read
$env:IDP_MCP_TOKEN = "<paste the one-time token>"
python scripts/mcp_demo_client.py --limit 10
```

The CLI prints the high-entropy bearer token once. The database stores only its SHA-256 hash, organization, scopes, and lifecycle timestamps. The default expiry is 30 days; the maximum is 365 days. Record the token UUID shown at issue time for revocation:

```powershell
docker compose exec -T api python -m app.mcp.cli revoke --token-id <token-uuid>
```

The demo client uses the official Python MCP SDK. It lists the four read-only tools and calls `list_invoices`; `--invoice-id <uuid>` also calls `get_invoice_summary`. Never paste the token into a prompt or commit it. Scopes are `invoices:list`, `invoices:summary`, `invoices:reconciliation`, and `purchase_orders:read`. The tools derive organization exclusively from the bearer token and use the same read services as REST. They return bounded JSON, no source files/raw extraction text, and do not provide approval or payment operations. REST remains JWT/membership/role authorized; MCP uses a separate, scoped service-token policy.

The dispatcher heartbeat advances only when both its durable outbox poll and expired-lease recovery pass. The worker health check sends a Celery ping. A red/unhealthy container should be inspected before accepting new uploads.

## Foundry extraction (M5)

The default remains `EXTRACTION_PROVIDER=fixture` and does not need Azure credentials. To opt into Foundry, configure the endpoint and deployment in the untracked `.env`; the endpoint must be the model inference URL ending in `/openai/v1/`, not a project endpoint. The deployment must support Chat Completions, images, and Structured Outputs.

```dotenv
EXTRACTION_PROVIDER=foundry
ALLOW_LIVE_MODEL_CALLS=true
FOUNDRY_MODEL_ENDPOINT=https://<resource>.services.ai.azure.com/openai/v1/
FOUNDRY_DEPLOYMENT=<deployment-name>
FOUNDRY_API_KEY=
```

The adapter uses `DefaultAzureCredential` when `FOUNDRY_API_KEY` is empty; grant the runtime identity the documented Foundry inference role. For local testing only, store an API key in `.env`, never in source control. Restart the API and worker after changing provider settings. No live request is made by health checks or fixture tests. Live calls may incur cloud charges; a model/deployment not supporting the requested schema or image capability fails with a typed provider error and never falls back to fixture output.

Run the labeled validation split only after configuring an actual endpoint, deployment, authentication, and explicit live opt-in:

```powershell
python scripts/evaluate_ai_corpus.py --split validation --live
```

Foundry provider-call rows use the existing PostgreSQL usage table, so this adapter adds no migration. The live state remains `LIVE_PENDING` until an actual credentialed validation run is performed and its report is inspected.

## Policy guidance lookup (M5b)

Migration 0004 adds organization-scoped, append-only policy source versions and page-level passages, plus a GIN full-text index. After upgrading, run the seed command to ingest seven synthetic demo sources for both demo organizations. Commands: docker compose exec -T api alembic upgrade head; docker compose exec -T api python -m app.seed; docker compose exec -T api alembic current.

The Hướng dẫn page uses the authenticated organization and does not accept an organization identifier from the query. It shows only exact passages from the latest version of each source and includes the source/version, section, page, and SHA-256 citations. Questions are not persisted or written to application logs.

## Invoice copilot (M5c)

The review-page copilot is disabled by default and does not affect fixture processing or policy lookup. It never emits canned answers as model output. To enable live Foundry tool calling, configure these values in the untracked `.env`, then rebuild/restart the API:

```dotenv
COPILOT_PROVIDER=foundry
ALLOW_LIVE_MODEL_CALLS=true
FOUNDRY_MODEL_ENDPOINT=https://<resource>.services.ai.azure.com/openai/v1/
FOUNDRY_DEPLOYMENT=<deployment-that-supports-function-calling>
```

The existing Foundry adapter uses `DefaultAzureCredential` when `FOUNDRY_API_KEY` is empty; local API keys are optional and must stay in `.env`. The model/deployment must support Chat Completions function calling. The endpoint must be the HTTPS model inference endpoint ending in `/openai/v1/`, not a project endpoint. Live requests can incur charges. Do not use a test-double result as live evidence.

The authenticated invoice route binds all reads to the invoice path and the actor's current organization. Tools are restricted to invoice summary, current reconciliation, its linked PO, bounded page evidence, and synthetic policy search. Before each tool read the service rechecks the current immutable version and reconciliation snapshot; if either changes, the turn stops. A turn allows at most four model rounds, six backend tool executions, and the configured deadline (`COPILOT_TIMEOUT_SECONDS`, default 45 seconds). Available input/output token counts and tool-call IDs/names/outcomes/timing appear in the response trace. Questions, prior messages, tool arguments/results, and reasoning traces are not persisted. No migration is needed because the copilot stores no conversation or trace rows.

To verify without cloud access, run the provider-contract and unit tests below; they inject explicitly named test doubles and do not fabricate a production answer. The authenticated `GET /api/v1/copilot/status` endpoint shows whether the live provider is enabled. A successful live tool-call trace is `LIVE_PENDING` until performed with an actual configured deployment.

```powershell
docker compose run --rm --no-deps api python -m pytest -q tests/unit/test_copilot_service.py tests/provider_contract/test_foundry_copilot_contract.py
```

Search uses PostgreSQL full-text search with accent-folded Vietnamese text. No embedding service or external model is called. The response is extractive rather than an LLM synthesis; weak or absent matches return “Chưa tìm thấy đủ nguồn”. The bundled sources are synthetic demo guidance, not real policy. Retrieved text cannot alter reconciliation, authorization, approval, or payment behavior.

To publish a new demo source version, add a manifest entry with the same source slug and the next sequential version number, then rerun the seed command. Never edit a version that has already been ingested: source and passage rows have database-level append-only triggers. The API refuses to reseed changed content under an existing version.

## M6 single-agent vs multi-agent experiment

The comparison runner, case handling, cost inputs, and report limits are documented in [the M6 experiment guide](m6-experiment.md). The runner is not part of the product workflow. Use `python scripts/run_copilot_experiment.py --preflight` first; this performs no database or model request. A live run needs a configured M5c Foundry deployment, explicit `--live --confirm-cloud-data`, a short-lived tenant-authenticated JWT, and nonfixture invoices. Fixture rows are blocked. The report excludes questions, answers, invoice IDs, and tool payloads; human-reviewed quality and citation entailment remain separate from automated source-ID checks.

## PostgreSQL, checkpoints, and review resume

The workflow thread ID is generated by the backend and is uniquely mapped to one job. Checkpoint state keeps internal IDs and small status fields; invoice files and provider raw output remain in private storage. Graph resume validates job, organization, invoice, the current immutable version, reconciliation snapshot hash, and the saved reviewer decision before finalization.

At human review, the graph interrupts and returns from the worker task. The job is `WAITING_REVIEW` and has no active lease while the reviewer works. The reviewer service writes the decision, approval record (for approval), and `RESUME_INVOICE_GRAPH` outbox event in its database transaction. The dispatcher sends the persisted decision ID; the graph cannot create or alter a reviewer decision. A PostgreSQL advisory lock serializes duplicate graph deliveries for one job and is released automatically if a worker process exits. Celery uses late acknowledgement and rejects tasks when a worker is lost, allowing delivery retry. Resume/finalization checks are idempotent.

Useful inspection commands:

```powershell
docker compose exec -T api alembic current
docker compose exec -T api python -m app.workflows.checkpoint_setup
docker compose logs --tail 100 dispatcher worker
```

The checkpoint setup command is idempotent. If a task is redelivered, let the worker process it; do not manually edit checkpoint tables or directly mark invoices approved. A stale decision/snapshot returns a conflict and must be reviewed against the current invoice version and reconciliation. The application domain service remains the only path that records approval.

## Migration and data recovery

Before upgrading a database that contains useful records, take and verify a PostgreSQL backup using the deployment's approved secret-handling method. For local development, this writes the dump outside the repository:

```powershell
New-Item -ItemType Directory -Force D:\idp-backups | Out-Null
docker compose exec -T postgres pg_dump -U idp -Fc idp > D:\idp-backups\idp-before-upgrade.dump
```

Then apply `alembic upgrade head` and run checkpoint setup as shown above. Do not run test integration commands against the demo database: `make test-integration` uses a separate `postgres-test` service with a temporary filesystem. `docker compose down -v` deletes PostgreSQL, Redis, and private document volumes; use it only when intentionally deleting all local data.

For a local restore, stop API/dispatcher/worker first, restore into a separately verified database/volume, and then run migrations and checkpoint setup. Do not overwrite an existing data volume as a routine recovery step.

## Verification commands

```powershell
make test-unit
make test-integration
make test-provider
make smoke
```

Integration tests require Docker and use PostgreSQL. `make test-integration` recreates only the `postgres-test` tmpfs container for each run and uses Redis database 15, so stale rows and login counters cannot contaminate the app database or demo Redis keys. Tests set `DOCUMENT_STORAGE_PATH` to an ephemeral container directory so synthetic uploads do not enter the private demo volume. Fixture regression and AI evaluation are separate; fixture outputs are never provider accuracy evidence. A live Bedrock evaluation remains `LIVE_PENDING` until the account, region, model access, and credentials are configured and an actual live run is recorded.

## Deployment status and limits

The repository includes a locally runnable Compose stack, migrations, checkpoint setup, operational health checks, smoke commands, and a local recovery runbook. This work has not deployed the app to a hosted environment. Public deployment still requires an explicitly selected hosting target, managed secret storage, TLS/ingress, production database and private object storage, backup/restore validation, monitoring, and account-specific provider configuration. Do not describe the local stack as a public or production deployment.

## Production Compose target

`docker-compose.production.yml` is a separate deployment target. It serves a compiled static frontend through versioned NGINX behind Caddy-managed HTTPS; API, dispatcher, and worker ports are not published. The application containers run as non-root with read-only root filesystems, reduced Linux capabilities, and temporary writable space. PostgreSQL and Redis are external managed services; uploaded bytes use a private S3 bucket. The development Compose stack is unchanged.

The production target is not a turnkey deployment until the operator has provisioned and tested the external services. Before starting it:

- Create a PostgreSQL database with TLS and an application-specific user. Set `sslmode=verify-full`, configure `POSTGRES_CA_FILE` to the provider CA bundle, and use `sslrootcert=/run/secrets/postgres-ca` in `DATABASE_URL`. The Compose target mounts that public CA bundle read-only into API, dispatcher, and worker containers.
- Create authenticated TLS Redis with HA appropriate to the queue workload. `REDIS_URL` must use `rediss://` and a strong password.
- Create a private S3 bucket in the selected region. Block all public access, enable bucket versioning, enforce server-side encryption, and grant only the application runtime role access to the bucket objects. Grant KMS encrypt/decrypt/data-key actions only when using a KMS key. Do not put static cloud credentials in the Compose file; use the host/task identity mechanism.
- Enable the selected Bedrock model or provision a Foundry deployment that supports the required extraction input and output schema. Live calls can incur charges. Fixture mode is rejected in production.
- Point DNS for `APP_HOST` to the host running Caddy, allow inbound TCP 80/443 (and UDP 443 for HTTP/3), and set a monitored ACME email. The first Caddy startup requests a public certificate.
- Store production environment values outside source control. `.env.production.example` is a placeholder-only template; its values are intentionally rejected by application configuration checks. Use a secret manager to inject values and never commit `.env.production`, credentials, invoice content, or provider outputs.
- Configure managed PostgreSQL backups and complete a restore drill; monitor API, dispatcher, worker, database, Redis, S3, provider errors, and queue age. Set resource limits from observed document sizes and concurrency.

For a non-secret local preflight, copy the template and replace every placeholder in a secure editor or secret injection workflow. The deployment environment needs `APP_HOST`, `ACME_EMAIL`, `DATABASE_URL`, `REDIS_URL`, `JWT_SECRET`, `S3_BUCKET`, `AWS_REGION`, and live provider settings. Keep `JWT_SECRET` high-entropy (for example, generate it with `python -c "import secrets; print(secrets.token_urlsafe(48))"`). URL-encode reserved characters in database/Redis passwords. The application refuses fixture extraction, demo seeding, local storage, non-TLS managed connections, and placeholder secrets in production.

Run from the repository root on a Linux Docker host with Docker Compose 2.33.1 or later and ports 80 and 443 available:

```powershell
Copy-Item .env.production.example .env.production
docker compose -f docker-compose.production.yml --env-file .env.production config --quiet
docker compose -f docker-compose.production.yml --env-file .env.production build --pull
docker compose -f docker-compose.production.yml --env-file .env.production run --rm --no-deps api python -c "from app.config import validate_runtime_settings; validate_runtime_settings()"
docker compose -f docker-compose.production.yml --env-file .env.production run --rm --no-deps api alembic upgrade head
docker compose -f docker-compose.production.yml --env-file .env.production run --rm --no-deps api python -m app.workflows.checkpoint_setup
docker compose -f docker-compose.production.yml --env-file .env.production up -d --wait
docker compose -f docker-compose.production.yml --env-file .env.production ps
$prodHost = ((Get-Content .env.production | Where-Object { $_ -like 'APP_HOST=*' } | Select-Object -First 1) -split '=', 2)[1]
curl.exe --fail --silent --show-error --output NUL "https://$prodHost/"
```

Create production organizations and human memberships through the restricted operator CLI after the stack is healthy. The CLI is not exposed as an API route, permits only the `operator` and `reviewer` roles, prompts for a new password without echoing it, hashes the password with the application hasher, never resets an existing password, and records organization-scoped provisioning events. Repeating an identical command is idempotent.

```powershell
docker compose -f docker-compose.production.yml --env-file .env.production exec api python -m app.provisioning_cli create-org --slug <org-slug> --name "<organization name>"
docker compose -f docker-compose.production.yml --env-file .env.production exec api python -m app.provisioning_cli create-user --email <operator@example.org> --org <org-slug> --role operator
docker compose -f docker-compose.production.yml --env-file .env.production exec api python -m app.provisioning_cli create-user --email <reviewer@example.org> --org <org-slug> --role reviewer
```

The migration and checkpoint commands are explicit. Take and verify a backup before migration on any database that contains useful records. The production command intentionally does not seed demo users or fixtures. Verify `/health/ready` from inside the API service and perform a reviewer walkthrough using an organization account created through the approved provisioning process. Before accepting documents, confirm that the fixture badge is absent only for live-provider records and that uploaded files never become public.

The current workspace has no production domain, managed PostgreSQL/Redis, private bucket, provider identity, human-provisioned user, restore drill, monitoring destination, or hosted release credentials. Therefore this is a **production deployment scaffold, not a deployment-verified service**; the repo is not currently a production deployment. Pin all built images to approved registry digests and run the organization’s vulnerability scan/signing process before releasing them.
