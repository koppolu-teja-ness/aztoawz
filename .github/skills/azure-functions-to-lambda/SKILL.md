---
name: azure-functions-to-lambda
description: Use when converting Azure Functions apps, triggers, bindings, identities, and settings into AWS Lambda plus event sources and scoped execution roles.
---
# Azure Functions to Lambda Skill

## Use This Skill When
- Source includes Function Apps and trigger/binding definitions.
- Target requires Lambda integration with API Gateway, EventBridge, S3, or SQS.

## Rules
- Runtime mapping MUST be explicit and version-aware.
- Trigger/binding translation MUST include event-source configuration.
- App settings map to environment variables with secret redaction controls.
- Managed identity MUST map to scoped IAM execution role.
- Every mapping MUST cite rule_id.

See mapping-reference.md for supported trigger mappings.