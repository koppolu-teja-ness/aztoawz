---
mode: agent
description: Generate migration plan, risk, execution, and validation reports.
---
# /generate-reports

## Inputs
- ${input:migration_plan_path}
- ${input:deployment_result_path}
- ${input:deployed_validation_report_path}

## Procedure
1. Assemble plan report.
2. Assemble risk report with score distribution and rationale.
3. Assemble execution report with timeline and status.
4. Assemble validation report with evidence summary.
5. Emit report bundle index and audit record.

## Output Artifact
- reports/
  - migration-plan.md
  - risk-report.md
  - execution-report.md
  - validation-report.md
  - report-index.json

## Stop Condition
Stop when all four reports and the index are generated.