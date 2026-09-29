---
mode: agent
description: Run static validation and security scanning on generated CloudFormation.
---
# /validate-static

## Inputs
- ${input:cfn_template_path}

## Procedure
1. Run cfn-lint and capture all findings.
2. Run checkov and capture policy findings.
3. Classify findings by severity and risk impact.
4. Propose remediations that do not weaken security posture.
5. Emit validation audit record.

## Output Artifact
- static_validation_report.json
  - cfn_lint_findings[]
  - checkov_findings[]
  - blocking_issues[]
  - remediation_plan[]
  - input_hash
  - timestamp

## Stop Condition
Stop when blockers and secure remediations are documented.