---
mode: agent
description: Build migration execution plan with sequencing and risk scoring.
---
# /plan-and-score

## Inputs
- ${input:mapping_table_path}
- ${input:static_validation_report_path}

## Procedure
1. Create dependency-aware execution sequence.
2. Score each item: Auto-Migratable, Requires Review, High Risk.
3. Include rationale and controlling findings/rules.
4. Build approval gate summary counts.
5. Emit plan hash and audit record.

## Output Artifact
- migration_plan.json
  - sequence[]
  - risk_scores[]
  - approval_gate_summary
  - plan_hash
  - input_hash
  - timestamp

## Stop Condition
Stop when plan hash and approval summary are generated for review.