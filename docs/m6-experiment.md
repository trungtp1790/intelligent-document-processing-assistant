# M6 single-agent vs multi-agent experiment

## Decision

The production review page continues to use the M5c single-agent copilot. M6 adds a local, opt-in comparison runner only; it does not add a product route or alter invoice processing. The multi-agent arm has three sequential roles: document reader, reconciliation explanation, and final explanation/citation checker. Existing tenant-scoped services supply all source material. No model can mutate invoices, reconciliation, reviewer decisions, or approvals.

The two arms receive the same question, current invoice version, reconciliation snapshot, Foundry deployment, temperature, per-completion output limit, and per-arm deadline. Both share a ceiling of four completion calls and six read-tool calls. The baseline can use up to those limits; the experimental arm plans three completion calls and performs four fixed backend reads. Both stop if the invoice or reconciliation snapshot changes. SDK retries are disabled.

## Data and reporting rules

- The runner rejects `fixture_mode` invoices before creating a provider request. Synthetic fixture results are not model-accuracy evidence.
- The operator supplies 1–20 cases using invoice IDs from one authenticated organization. `sample_id` is the only sample identifier stored in the report.
- A case manifest contains `sample_id`, `invoice_id`, and `question`; keep it under ignored `var/` and never commit private invoice identifiers or questions.
- The runner uses the existing current-principal membership check and derives organization from the bearer token. It does not accept `org_id` in the case manifest.
- Model answers, questions, document contents, evidence, and tool outputs are held in process memory and omitted from the JSON report. The report contains status, failure codes, role names, timing, tokens, budgets, and explicit configuration only.
- `citation_ids_valid` means the returned source IDs existed in the current snapshot. It does not measure whether the cited source entails the claim.
- `answer_quality` remains `NOT_MEASURED` until a human reviewer scores both arms against the same case. Use [the review template](copilot_m6_review_template.csv); do not fill it with generated or inferred scores.
- In the review template, correctness is `0=wrong, 1=partly correct, 2=correct`; groundedness, citation entailment, and abstention are each `0=no, 1=yes`. Review both arms against the same source snapshot and rubric.
- By default, no question or model answer is written to disk. To enable human scoring, add `--persist-private-review-bundle`; this writes a separate file under ignored `var/copilot_m6_reviews/` with questions, both answers, citation IDs, deterministic issue codes, and bounded evidence quotes. Restrict access to the local workspace and do not commit or share that file. The aggregate report still contains metadata only.
- Costs remain null unless the operator supplies both input and output rates. The runner does not guess current pricing.
- Only run with a Foundry deployment whose model family is recorded in the report context. The runner records the deployment alias but will not infer a model family from that alias.

## No-network preflight

The preflight writes configuration blockers and `LIVE_PENDING` without opening the database, validating a token, or contacting Foundry:

```powershell
docker compose run --rm `
  -v "${PWD}/evals/reports:/app/evals/reports" `
  api python scripts/run_copilot_experiment.py --preflight
```

For a one-case local run, create a private JSON file such as `var/copilot_m6_cases.json`:

```json
{
  "cases": [
    {
      "sample_id": "case-01",
      "invoice_id": "<invoice-uuid-from-the-current-organization>",
      "question": "Explain the current reconciliation issue and cite the source pages."
    }
  ]
}
```

The placeholder above is not an executable sample and must be replaced with an eligible nonfixture invoice. The current repository has no labeled nonfixture invoice QA corpus.

## Credentialed live run

First configure the existing M5c values in the untracked `.env` (`COPILOT_PROVIDER=foundry`, `ALLOW_LIVE_MODEL_CALLS=true`, Foundry endpoint and deployment). Export a short-lived operator/reviewer JWT obtained through the normal login flow into `IDP_COPILOT_EXPERIMENT_TOKEN` without printing it. Review and authorize the selected nonfixture documents for cloud processing. Then run:

```powershell
$reportDir = (Resolve-Path .\evals\reports).Path
$caseFile = (Resolve-Path .\var\copilot_m6_cases.json).Path
docker compose run --rm `
  -e IDP_COPILOT_EXPERIMENT_TOKEN `
  -v "${reportDir}:/app/evals/reports" `
  -v "${caseFile}:/tmp/copilot_m6_cases.json:ro" `
  api python scripts/run_copilot_experiment.py `
    --live --confirm-cloud-data --persist-private-review-bundle `
    --model-family "<actual deployed model family>" `
    --case-file /tmp/copilot_m6_cases.json `
    --output /app/evals/reports/copilot_m6_latest.json
```

If the user token, provider, live flag, endpoint, deployment, model family, or explicit cloud-data confirmation is absent, the runner blocks before a provider call and records the reason. `--confirm-cloud-data` is required because the experiment sends invoice summary and page evidence to the configured provider. The private review bundle flag is separately required to retain answers/evidence for manual scoring.

To calculate a cost estimate, supply the rates approved for the deployed model on that run:

```powershell
--input-price-usd-per-million <input-rate> `
--output-price-usd-per-million <output-rate>
```

No held-out accuracy run is performed by this command. Inspect the report for `sample_count`, failure codes, tokens, latency, tool calls, cost, and the M6 role traces. Score output manually using the same rubric for each arm. Only consider adding a multi-agent product mode after a meaningful improvement is observed across the same eligible cases and budget; otherwise retain the single-agent product path.

## Current result

See [the latest report](../evals/reports/copilot_m6_latest.json). A preflight or blocked run is reported as `LIVE_PENDING`, with zero model responses and `answer_quality_status=NOT_MEASURED`. Test doubles used by unit tests verify orchestration only and are never included as evaluation samples or model responses.
