---
mode: agent
description: Deploy approved plan only after artifact verification and track status by dependency order.
---
# /deploy-approved

## Inputs
- ${input:migration_plan_path}
- ${input:approval_artifact_path}
- ${input:cfn_template_path}

## Procedure
1. Verify signed approval artifact matches plan_hash.
2. Refuse deployment if artifact missing, invalid, or mismatched.
3. Deploy resources in dependency order.
4. Track per-resource status and failures.
5. Execute rollback procedure on blocking failures.
6. Emit deployment audit record.

## Output Artifact
- deployment_result.json
  - plan_hash
  - approval_verification
  - resource_statuses[]
  - rollback_actions[]
  - input_hash
  - timestamp

## Stop Condition
Stop when deployment completes or rollback finishes with full status record.