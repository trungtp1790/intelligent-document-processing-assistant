# Invoice Review Assistant

An invoice review workflow built with FastAPI, PostgreSQL, React, Celery, Redis, and LangGraph. It takes an uploaded document through extraction, immutable review versions, deterministic purchase-order reconciliation, and a reviewer-controlled decision.

## Implemented

- Tenant-scoped authentication and private document storage.
- Fixture processing that runs without a cloud API key and is labeled as synthetic in the UI.
- Optional Bedrock and Microsoft Foundry extraction adapters with page-level provenance and typed provider failures.
- PostgreSQL-backed workflow checkpoints, durable job processing, idempotent mutations, and review resume after restart.
- Read-only MCP tools, versioned policy lookup, and an opt-in single-agent invoice copilot.
- An isolated M6 runner that compares the production single-agent baseline with three sequential read-only roles. The experiment does not alter the production copilot or expose approval tools.

## Local verification

The latest recorded checks include 15 PostgreSQL integration tests and 68 unit/provider tests passing. The fixture rule regression passes 12/12 synthetic cases; this is plumbing and rule evidence, not AI accuracy evidence. See [the progress log](progress.md) for commands and verification history.

The latest M6 preflight is `LIVE_PENDING`: zero eligible cases, zero provider attempts, zero model responses, and `NOT_MEASURED` answer quality. Foundry is not configured in this workspace. The bundled evaluation corpus is synthetic and is not used as nonfixture M6 QA data.

## Run the local demo

From Windows PowerShell with Docker Desktop and Node.js installed:

```powershell
python scripts/bootstrap.py
docker compose up --build -d --wait
docker compose exec api alembic upgrade head
docker compose exec api python -m app.workflows.checkpoint_setup
docker compose exec api python -m app.seed
python scripts/smoke.py --demo-login
```

Open <http://localhost:5173>. No model API key is needed for fixture mode. The generated `.env` contains the local demo password; do not commit it.

## Limits and references

This project has not been deployed to a hosted environment. No live Bedrock or Foundry extraction/copilot request has been verified. The M6 human quality comparison and a 3–5 minute video recording remain pending; the [demo script](demo-video-script.md) is prepared. Synthetic fixture or corpus results must not be presented as real-invoice AI accuracy.

- [Architecture and security boundaries](architecture.md)
- [M6 experiment protocol and reproducibility](m6-experiment.md)
- [Latest M6 preflight report](../evals/reports/copilot_m6_latest.json)
- [Implementation progress and test results](progress.md)
