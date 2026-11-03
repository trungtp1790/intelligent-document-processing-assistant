# Intelligent Document Processing Assistant

This repository documents an invoice review assistant with the following required behavior:

## Core product requirements

- Tenant-scoped login
- Private document uploads
- Durable background jobs
- Extraction paths using:
  - Synthetic fixtures for deterministic plumbing/rule checks
  - Optional Amazon Bedrock extraction
  - Optional Azure AI Foundry extraction
- Immutable document revisions
- Purchase-order reconciliation
- Reviewer decision workflow
- LangGraph-based review pause/resume with PostgreSQL checkpoints

## Extension points

- Read-only MCP integration
- Policy lookup integration
- Single-agent Copilot integration

## Important validation note

Synthetic fixtures in this project verify integration plumbing and rule behavior only. They are **not** evidence of live AI extraction accuracy.
