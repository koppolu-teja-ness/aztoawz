---
name: secret-safe-llm-handling
description: Use when designing prompts, logs, traces, fixtures, or tests to enforce that secret values never enter LLM-visible paths.
---
# Secret-Safe LLM Handling Skill

## Use This Skill When
- Building agent prompts, traces, logs, tests, or telemetry.

## Rules
- Secret values MUST NOT enter LLM context, logs, traces, fixtures, snapshots, or reports.
- Only metadata (name/version/tags/access scope) may pass through agent state.
- Value transfer MUST occur via direct SDK calls outside prompt path.
- Redaction filters MUST be tested and enabled by default.

See redaction-patterns.md for allowed/forbidden examples.