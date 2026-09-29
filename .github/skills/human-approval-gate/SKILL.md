---
name: human-approval-gate
description: Use when creating, validating, or enforcing human approval artifacts and plan-hash checks before any deployment action.
---
# Human Approval Gate Skill

## Use This Skill When
- A deployment decision point is reached.
- Approval artifact schema/API/UI behavior is being implemented or reviewed.

## Rules
- Deployment MUST be blocked without signed approval artifact.
- Artifact MUST include plan_hash and reviewer metadata.
- Approve/Reject/Modify decisions MUST be auditable.

See approval-artifact-schema.md for required fields.