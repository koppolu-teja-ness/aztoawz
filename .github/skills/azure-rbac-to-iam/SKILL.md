---
name: azure-rbac-to-iam
description: Use when translating Azure RBAC assignments, scopes, and permissions into AWS IAM policies, roles, and trust relationships with least-privilege guardrails.
---
# Azure RBAC to IAM Skill

## Use This Skill When
- Migration includes role assignments or access policy semantics.

## Rules
- Preserve scope granularity from Azure to AWS resources.
- Avoid wildcard actions/resources unless explicitly justified and High Risk.
- Attach policies to explicit principals only.
- Add conditions for contextual restrictions where feasible.
- Cite rule_id for each mapping.

See guardrails.md for review checks.