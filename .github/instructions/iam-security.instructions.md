---
applyTo:
	- "src/**"
	- "infra/**"
	- "**/*.policy.json"
	- "**/*.cfn.yaml"
---
# IAM and Security Instructions

## Objective
Enforce least privilege and non-regressive security posture during migration.

## MUST Rules
- MUST scope Action and Resource to minimum necessary set.
- MUST map Azure RBAC/access policy semantics to principal-specific IAM policies.
- MUST define trust policies for managed-identity equivalents explicitly.
- MUST use condition keys where contextual restriction is possible.
- MUST apply permission boundaries where role expansion risk is present.
- MUST fail review on Action:* or Resource:* unless explicitly rule-justified and tagged High Risk.
- MUST run checkov and treat failing policy checks as blockers unless documented exceptions exist.

## MUST NOT Rules
- MUST NOT broaden internet exposure relative to source.
- MUST NOT approve deployment with unresolved High Risk security findings.

## Review Checklist
- Principal scoping verified
- Resource scoping verified
- Conditions present for high-impact actions
- checkov findings triaged and documented

See azure-rbac-to-iam and secret-safe-llm-handling skills for mapping and data-handling constraints.