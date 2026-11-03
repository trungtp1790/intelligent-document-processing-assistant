# Portfolio demo video script (about 4 minutes)

## Recording setup

- Use the local Compose stack and the seeded `demo-a` organization.
- Show the fixture badge before opening a sample. Every document shown is synthetic demo data.
- Keep `.env`, passwords, access tokens, database consoles, and document storage paths out of the recording.
- Do not describe fixture checks as AI accuracy, show a fake copilot answer, or call the local Compose stack a hosted deployment.
- Record at 1080p or higher with the browser zoom set so the fixture badge and status text are readable.

## Script

### 0:00–0:25 — What the product does

**On screen:** Sign-in page, then the document inbox.

**Narration:** “This is an invoice review assistant built around a controlled workflow. It accepts a document, extracts candidate fields, compares them with a purchase order using application rules, and leaves the final approval to a reviewer.”

### 0:25–0:55 — Local demo and fixture label

**On screen:** Sign in with the local demo operator. Keep the fixture label visible in the header and sample area.

**Narration:** “This run uses synthetic fixtures and does not need a cloud API key. The label is persistent because these records demonstrate plumbing and rule behavior, not live model accuracy.”

### 0:55–1:35 — Document intake and processing

**On screen:** Open the sample gallery and select the two-page invoice sample. Show its queue/status transition and document preview.

**Narration:** “Uploads are validated and stored privately. Jobs are durable and idempotent. Workers use a PostgreSQL-backed workflow checkpoint; at human review the worker releases its lease and waits for the reviewer service to persist a decision.”

### 1:35–2:20 — Review and evidence

**On screen:** Open the invoice review page. Show fields, page navigation, evidence labels, reconciliation issues, and version history.

**Narration:** “The page shows the current immutable invoice version and deterministic reconciliation result. Evidence points to a source page. Scanned-image evidence is marked as unverified until a person checks the source. Editing creates a new version instead of overwriting history.”

### 2:20–2:55 — Reconciliation and permissions

**On screen:** Show the linked purchase order and reconciliation issue codes. If safe, switch to the reviewer demo account and show the decision controls without submitting a decision.

**Narration:** “Tenant scope comes from the signed membership. The model cannot approve, reject, or change rule outcomes. The reviewer service checks the current version and reconciliation snapshot before it records a decision.”

### 2:55–3:25 — Read-only integrations

**On screen:** Show the policy guidance citations and, if configured for the recording, the MCP tool list. Do not display token plaintext.

**Narration:** “Policy lookup returns exact synthetic source/version citations and abstains when evidence is weak. MCP exposes a small, scoped read-only tool set. Neither integration has approval or payment actions.”

### 3:25–3:50 — Copilot and experiment boundary

**On screen:** Show the copilot disabled state, then the architecture diagram or M6 report status.

**Narration:** “The copilot is opt-in and has bounded read tools. The multi-agent comparison is a separate experiment runner, not part of the production workflow. Live Foundry credentials and a human-reviewed evaluation set are not configured in this demo, so model quality and the multi-agent comparison remain unmeasured.”

### 3:50–4:05 — Reproduction

**On screen:** Show the short commands in README, without opening `.env`.

**Narration:** “The repository includes the local startup, migration, smoke, and test commands. This is a locally verified Compose demo, not a public deployment.”

## End card

“Invoice review assistant · PostgreSQL · FastAPI · React · LangGraph · optional Bedrock/Foundry · synthetic demo data”
