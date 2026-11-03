# Intelligent Document Processing Assistant

Invoice review assistant with tenant-scoped login, private uploads, durable jobs, fixture and opt-in Bedrock/Foundry extraction, immutable revisions, purchase-order reconciliation, reviewer decisions, and LangGraph review pause/resume with PostgreSQL checkpoints. Read-only MCP, policy lookup, and the single-agent copilot are documented extensions. Synthetic fixtures verify plumbing and rule behavior only; they are not evidence of live AI accuracy.

## Portfolio and architecture

- [Short portfolio summary](docs/portfolio.md)
- [System architecture and security boundaries](docs/architecture.md)
- [Four-minute demo recording script](docs/demo-video-script.md)
- [M6 single-agent vs multi-agent protocol](docs/m6-experiment.md)
- [Latest M6 experiment report](evals/reports/copilot_m6_latest.json)
- [Milestone progress and verification log](docs/progress.md)

The M6 comparison runner is isolated from the product path. Its current report is `LIVE_PENDING` because this workspace has no configured Foundry deployment or credential. It excludes fixture invoices and leaves answer quality `NOT_MEASURED`; no model output or accuracy figure is fabricated.

## Run locally (Windows PowerShell)

Requirements: Docker Desktop with Linux containers, Python 3.12+, and Node.js 20+. No API key is needed for fixture mode.

```powershell
python scripts/bootstrap.py
docker compose up --build -d --wait
docker compose exec api alembic upgrade head
docker compose exec api python -m app.workflows.checkpoint_setup
docker compose exec api python -m app.seed
python scripts/smoke.py --demo-login
```

Open <http://localhost:5173>. Read the generated `.env` for the one-time local demo password. Sign in as `operator@demo.local` or `reviewer@demo.local` (choose organization `demo-a`). The seed also creates a second isolated organization (`demo-b`). The local password and JWT secret are generated locally and are not committed. The UI contains 12 synthetic DEMO samples, including scans, a two-page invoice, rule mismatches, missing fields, and an untrusted prompt-injection example. Fixture mode accepts only those exact manifest samples. Arbitrary invoice uploads are rejected before persistence until a live extraction provider is configured.

Running `python -m app.seed` synchronizes the seeded demo accounts with the current `DEMO_PASSWORD` in `.env`, including when the accounts already exist.

To stop without deleting local data: `docker compose down`. The PostgreSQL data, Redis queue and private files use separate named volumes. Runtime uploads stay in the private `documents` volume and are never served as static web content.

## Verify

```powershell
docker compose exec api pytest -q
docker compose exec api ruff check app tests
docker compose exec frontend npm run build
docker compose exec frontend npm test
docker compose exec api python scripts/evaluate_fixtures.py
python scripts/smoke.py --demo-login
```

Migrations and LangGraph checkpointer setup are explicit and are not run by API replicas. PostgreSQL is the supported database; SQLite is not used as a substitute for its constraints/transaction behavior. Checkpoint setup uses the package-managed PostgreSQL saver schema.

For health checks, crash/restart behavior, backup/restore, and operator commands, see [the operations runbook](docs/runbook.md). A separate production Compose target builds a static frontend and HTTPS ingress against external managed services; it still requires real cloud credentials and has not been deployed to a hosted environment.

## Read-only MCP demo (M5a)

The official Python MCP SDK exposes a Streamable HTTP server at `http://localhost:5173/api/v1/mcp` through the frontend's same-origin proxy. The API itself remains private to the Compose network. Issue a scoped, organization-bound service token from the local API container; token plaintext is displayed once and only its hash is stored:

```powershell
docker compose exec -T api python -m app.mcp.cli issue --org demo-a --name "local read-only demo" --scope invoices:list --scope invoices:summary --scope invoices:reconciliation --scope purchase_orders:read
$env:IDP_MCP_TOKEN = "<paste the one-time token>"
python scripts/mcp_demo_client.py --limit 10
```

The independent client lists available MCP tools and calls `list_invoices`; pass `--invoice-id <uuid>` to also call `get_invoice_summary`. It does not print the bearer token. Available scopes are `invoices:list`, `invoices:summary`, `invoices:reconciliation`, and `purchase_orders:read`. Tokens expire after 30 days by default, can be limited to 365 days, and can be revoked with `docker compose exec -T api python -m app.mcp.cli revoke --token-id <uuid>`.

MCP is read-only and returns bounded fields from the same tenant-scoped application services as REST. It does not expose document bytes/raw evidence and has no approve, reject, or payment tool. REST endpoints remain business APIs using user JWT plus organization membership and reviewer role checks; MCP uses separately issued organization-bound service tokens with per-tool read scopes. See the runbook and [the M5a decision](docs/decisions.md#2026-10-27--m5a-read-only-mcp) for token handling and the SDK/version rationale.

## Demo samples and limits

Choose one of the 12 bundled synthetic samples in the UI. Each file is labeled `DEMO` and is mapped by SHA-256 to its own fixed fixture result. Fixture mode rejects an unlisted upload with `FIXTURE_NOT_FOUND` before persisting it; the UI directs users to bundled samples and disables arbitrary uploads while the fixture library is active. It never falls back to a fabricated success. The UI displays **Fixture demo — dữ liệu trích xuất có sẵn** for fixture-derived records.

The project is not deployed. Bedrock and Foundry adapters are implemented, but live calls remain **LIVE_PENDING**: the active workspace is in fixture mode, has no provider endpoint/model deployment configured, and keeps live calls disabled. No live request has been made. Fixture mode and synthetic policy lookup run without an API key. The app supports PDFs and PNG/JPEG within configured limits. Password-protected/corrupt PDFs and unsupported files are rejected at upload. Provider evidence is page-level; exact bounding boxes are not claimed.

## Environment

`scripts/bootstrap.py` creates an untracked `.env` with random `JWT_SECRET` and `DEMO_PASSWORD`. Do not reuse local demo credentials outside development. `EXTRACTION_PROVIDER=fixture` and `ALLOW_LIVE_MODEL_CALLS=false` are safe defaults. PostgreSQL and Redis are private to the compose network and are not published to the host.

## Bedrock (opt-in)

To use a real provider, set `EXTRACTION_PROVIDER=bedrock`, `ALLOW_LIVE_MODEL_CALLS=true`, `AWS_REGION`, and a compatible `BEDROCK_MODEL_ID` in `.env`, and configure AWS credentials through the SDK credential chain or a runtime role. The adapter uses Bedrock Converse tool-schema output, records each physical call, and allows at most three invocations per extraction run. It does not fall back to fixtures when configuration or calls fail. Live calls may incur cloud charges.

The adapter sends usable searchable PDF text and renders low-text PDFs and images for vision input. Evidence quotes are checked against cited page text when available; image page references remain explicitly unverified until a reviewer confirms them. The default provider read timeout is 180 seconds and the total job deadline is 600 seconds; both are configurable.

The checked SDK is boto3 1.43.118. Amazon Nova Lite (`amazon.nova-lite-v1:0`) is a compatible candidate where available, including image input and client-side tool use, but the repository does not hard-code a model or region. Confirm regional availability and account access before enabling live calls. The adapter uses tool-schema output plus application-side Pydantic validation; it does not claim native structured-output mode. The current `.env.example` keeps the region/model empty and fixture mode enabled.

## Microsoft Foundry (opt-in)

The Foundry adapter uses the pinned OpenAI Python SDK against the model inference endpoint ending in `/openai/v1/`; `FOUNDRY_DEPLOYMENT` is the deployed alias passed as the request model. Configure `FOUNDRY_MODEL_ENDPOINT`, `FOUNDRY_DEPLOYMENT`, `EXTRACTION_PROVIDER=foundry`, and `ALLOW_LIVE_MODEL_CALLS=true` in the untracked `.env`. A deployment must support chat completions, image input, and Structured Outputs. Entra authentication through `DefaultAzureCredential` is used when no local `FOUNDRY_API_KEY` is set. No project endpoint, model, region, or account is selected in this repo. See [the Foundry runbook](docs/runbook.md#foundry-extraction-m5) and [the M5 decision](docs/decisions.md#2026-10-27--m5-foundry-extraction-adapter).

## Policy guidance lookup (M5b)

The Hướng dẫn page searches seven synthetic demo sources, scoped to the signed-in organization. The current implementation uses PostgreSQL full-text search and returns exact passages with source version, section, page, and SHA-256 hashes. It does not call an embedding or generation model; the answer is extractive and abstains when the sources do not match the question. Every source is labeled DEMO and does not represent real accounting, tax, legal, or company policy.

## Invoice copilot (M5c, opt-in)

The invoice review page includes a read-only copilot. It uses the Foundry Chat Completions function-calling API only when `COPILOT_PROVIDER=foundry`, `ALLOW_LIVE_MODEL_CALLS=true`, and a Foundry inference endpoint/deployment are configured. The deployment must support function calling. The default `COPILOT_PROVIDER=disabled` keeps fixture review, upload, and policy lookup available without an API key; the UI explicitly says when live chat is unavailable and never substitutes a canned model answer.

The server offers only invoice summary, reconciliation, the already-linked PO, bounded page evidence, and policy lookup tools. IDs and organization are bound by the authenticated route and current invoice; the model cannot choose another invoice or organization. The server validates each tool name/argument, echoes the model's exact tool-call ID in its result message, checks the invoice version and reconciliation snapshot before every read, and enforces at most four model rounds, six backend tool executions, and a 45-second default deadline. It does not expose approve/reject/export/payment tools. The response trace contains call names/IDs, outcomes, timing, and available token usage; chat messages, tool arguments/results, and chain-of-thought are not persisted. Live use can incur model charges.

The Foundry copilot shares the pinned official OpenAI Python SDK and the existing endpoint/credential construction with the Foundry extraction adapter; configure it as described in [the runbook](docs/runbook.md#invoice-copilot-m5c). Offline contract and orchestration tests use explicit test doubles only; they do not establish a live model tool-call trace. Live chat remains `LIVE_PENDING` until a credentialed request and trace are verified.

After applying the migrations, run docker compose exec -T api python -m app.seed to load the sources for both demo organizations. Each source version and passage is append-only. Add a new version as a new manifest entry with the next sequential version number; never change content under an already ingested version. Retrieved guidance cannot change reconciliation rules or reviewer decisions.

## Fixture rule report

`python scripts/evaluate_fixtures.py` verifies all 12 hashes and compares declared expected findings with deterministic reconciliation rules. It is a rule regression check, not an extraction accuracy report. Its results are not included in AI accuracy.

## AI extraction evaluation

`evals/datasets/synthetic_invoice_corpus_v1/` contains 90 generated, watermarked PDFs with separate SHA-verified programmatic reference labels. These labels are not human annotated, and the corpus is not evidence of performance on real invoices. The grouped split is 30 dev samples (3 families), 20 validation samples (2 families), and 40 held-out test samples (4 families); layout families and source document IDs cannot cross splits. The runner verifies hashes, metadata, PDF validity, split isolation, and label schemas before scoring. Fixture samples and post-review/manual-correction values are excluded.

Use the host Python environment to keep generated reports on the host:

```powershell
python scripts/evaluate_ai_corpus.py --split validation --live
```

The run requires `ALLOW_LIVE_MODEL_CALLS=true` and either a fully configured Bedrock provider or Foundry endpoint/deployment. Without live configuration, the report records blocked samples and leaves AI accuracy `NOT_MEASURED`; it never substitutes fixture predictions. A held-out run requires an explicit confirmation and consumes the test split once a model input is attempted:

```powershell
python scripts/evaluate_ai_corpus.py --split test --confirm-held-out --live
```

Reports are written to `evals/reports/`; raw predictions are private under ignored `var/eval_predictions/`.

