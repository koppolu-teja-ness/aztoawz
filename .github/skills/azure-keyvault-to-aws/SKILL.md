---
name: azure-keyvault-to-aws
description: Use when migrating Azure Key Vault resources, policies, keys, secrets, or certificates into AWS Secrets Manager, KMS, ACM, and scoped IAM with secret-safe handling.
---
# Azure Key Vault to AWS Skill

## Use This Skill When
- Source contains Key Vault secrets, keys, certs, RBAC, or access policies.
- Migration requires selecting Secrets Manager, KMS, ACM equivalents.

## Rules
- Secret values MUST NOT enter prompts, logs, traces, or tests.
- Access mappings MUST produce scoped IAM principals and policies.
- Soft-delete/purge-protection intent MUST map to recovery and deletion controls.
- Every mapping row MUST include rule_id.

## Outputs
- Mapping table rows with rule_id and confidence.
- Manual-handling flags when parity is unavailable.

See reference.md for construct mapping table.