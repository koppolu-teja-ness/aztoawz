---
applyTo:
	- ".github/workflows/**"
	- "Dockerfile*"
---
# CI/CD and Docker Instructions

## Objective
Create secure and repeatable pipelines for validation and controlled deployment.

## Pipeline Stages
1. Lint and type checks
2. Unit and integration tests
3. CloudFormation validation (cfn-lint)
4. Security scan (checkov)
5. Plan and risk artifact generation
6. Human approval gate verification
7. Controlled deployment
8. Post-deploy validation and reporting

## MUST Rules
- MUST use OIDC federation to AWS; long-lived access keys are prohibited.
- MUST use least-privilege deploy role.
- MUST enforce required status checks before merge/deploy.
- MUST publish audit artifacts and validation reports.
- MUST fail pipeline when approval artifact is absent or invalid.

## Docker Guidance
- Pin base images to supported minor versions.
- Use non-root runtime where practical.
- Keep image free of secrets and environment-specific credentials.

See cfn-authoring-and-lint and review-security prompt for policy-aligned enforcement.