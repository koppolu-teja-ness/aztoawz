---
mode: agent
description: Execute the full migration workflow end-to-end with a mandatory pause at human approval.
---
# /migrate-e2e

## Inputs
- ${input:subscription_id}
- ${input:resource_group}
- ${input:bicep_path}
- ${input:target_region}

## Procedure
1. Run discover, parse, map, generate, and validate-static stages.
2. Build plan and risk score outputs.
3. Pause at approval gate and require Approve/Reject/Modify decision.
4. If approved, run deploy and validate-deployed.
5. Generate reports and final audit bundle.

## Output Artifact
- e2e_migration_bundle.json
  - stage_outputs
  - plan_hash
  - approval_record
  - deployment_result
  - validation_report
  - report_index

## Stop Condition
Stop after approval outcome is applied and final bundle is emitted.