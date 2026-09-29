---
mode: agent
description: Perform PR-level security review against mandatory migration rules.
---
# /review-security

## Inputs
- ${input:change_scope}
- ${input:artifacts_path}

## Procedure
1. Review IAM scope, trust policies, and wildcard usage.
2. Verify secret-safe handling in prompts/logs/traces/tests.
3. Verify approval-gate enforcement and plan-hash linkage.
4. Verify security-posture non-regression claims.
5. Return pass/fail with blocking findings.

## Output Artifact
- security_review_report.json
  - status
  - blocking_findings[]
  - non_blocking_findings[]
  - required_remediations[]
  - timestamp

## Stop Condition
Stop when report includes explicit pass/fail and blocking rationale.