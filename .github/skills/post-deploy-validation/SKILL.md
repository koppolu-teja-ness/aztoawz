---
name: post-deploy-validation
description: Use when verifying deployed AWS resources against source intent using equivalence checks, smoke tests, and security-posture diff.
---
# Post-Deploy Validation Skill

## Use This Skill When
- Deployment has completed and correctness/security validation is needed.

## Rules
- Run structural equivalence checks per migrated service.
- Run functional smoke tests for expected behavior.
- Run security-posture diff and fail on broadened exposure/permissions.
- Record evidence for each pass/fail result.

See validation-recipes.md for checklists by service.