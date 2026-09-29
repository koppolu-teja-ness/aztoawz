---
applyTo:
  - "src/api/**"
  - "src/ui/**"
---
# API and UI Instructions

## Objective
Provide secure approval and visibility interfaces for migration planning and execution.

## API Requirements (FastAPI)
- Define explicit request/response models.
- Expose approval endpoints for Approve, Reject, and Modify.
- Enforce authentication and authorization for approval actions.
- Record reviewer identity, timestamp, decision, rationale, and plan hash.

## UI Requirements
- Show approval gate summary counts:
  - Auto-Migratable
  - Requires Review
  - High Risk
- Provide explicit Approve/Reject/Modify actions.
- Display rule IDs, risk rationale, and affected resources.

## MUST Rules
- MUST not expose secret values in UI payloads.
- MUST block deploy action when approval artifact is missing or hash-mismatched.
- MUST preserve auditability for every decision event.

See human-approval-gate and migration-reporting skills for schema and report alignment.