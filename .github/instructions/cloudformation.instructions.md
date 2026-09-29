---
applyTo:
	- "**/*.cfn.yaml"
	- "infra/cfn/**"
	- "src/agents/generator/**"
---
# CloudFormation Authoring Instructions

## Objective
Generate valid, secure, and lint-clean AWS CloudFormation YAML.

## Template Conventions
- YAML only.
- Include AWSTemplateFormatVersion.
- Use Parameters, Mappings, and Conditions intentionally.
- Apply standard tags to all supported resources.
- Use DeletionPolicy and UpdateReplacePolicy for stateful resources.
- Default to KMS encryption where applicable.
- Never hardcode account IDs, ARNs, or region-specific constants when parameters/mappings fit.
- Use stable cross-stack export names with explicit prefixes.

## MUST Rules
- MUST pass cfn-lint before outputs are accepted.
- MUST NOT invent properties or unsupported resource types.
- MUST include KB rule IDs in generation trace metadata.
- MUST emit UNMAPPED placeholders for unresolved constructs.
- MUST preserve least-privilege IAM relationships.

## Good and Bad
Good:
- Parameterized KMS key ARN with condition-based fallback.

Bad:
- Literal account-scoped ARN embedded in multiple resources.

See cfn-authoring-and-lint skill for tagging schema and lint remediation patterns.