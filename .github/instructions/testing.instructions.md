---
applyTo: tests/**
---
# Testing Instructions

## Objective
Prove migration correctness, safety, and policy compliance through deterministic tests.

## Required Test Types
- Unit tests for parser, mapper, generator, validator.
- Golden-file tests for Bicep-to-CloudFormation transformations.
- Property-based tests for mapping invariants.
- moto-backed AWS interaction tests where applicable.
- cfn-lint validation tests for generated templates.
- Mandatory secret-safety test: verify no secret values in prompts, logs, traces, fixtures, or snapshots.

## MUST Rules
- MUST fail CI on regression in security posture diff.
- MUST include tests for UNMAPPED behavior when no KB rule matches.
- MUST include tests for approval-gate refusal without signed artifact.
- MUST keep fixtures secret-free.

## Tooling
- pytest
- hypothesis (for property-based tests)
- moto
- cfn-lint

See post-deploy-validation and secret-safe-llm-handling skills for test recipes.