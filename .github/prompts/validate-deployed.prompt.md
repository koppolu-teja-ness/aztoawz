---
mode: agent
description: Validate deployed AWS resources for structural, functional, and security equivalence.
---
# /validate-deployed

## Inputs
- ${input:deployment_result_path}
- ${input:source_inventory_path}

## Procedure
1. Run structural equivalence checks against source intent.
2. Run smoke tests for mapped services.
3. Perform security-posture diff (public exposure and IAM breadth).
4. Record pass/fail with evidence.
5. Emit validation audit record.

## Output Artifact
- deployed_validation_report.json
  - structural_checks[]
  - functional_smoke_tests[]
  - security_posture_diff[]
  - equivalence_status
  - input_hash
  - timestamp

## Stop Condition
Stop when equivalence status and security diff are fully documented.