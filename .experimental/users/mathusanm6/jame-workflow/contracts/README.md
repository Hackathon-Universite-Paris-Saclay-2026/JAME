# Contracts

This folder contains the shared API and event contracts used by:
- Python backend orchestrator
- VS Code extension client

## Files
- `runs.openapi.yaml`: REST lifecycle contract.
- `events.schema.json`: WebSocket event schema.

Keep these contracts backward-compatible and versioned before extending payloads.
