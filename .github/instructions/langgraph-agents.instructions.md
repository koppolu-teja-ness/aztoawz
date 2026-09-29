---
applyTo:
	- "src/agents/**"
	- "src/graph/**"
---
# LangGraph Agent Instructions

## Objective
Build reliable multi-agent orchestration with typed state, traceability, and human gating.

## Design Requirements
- One LangGraph node per agent responsibility.
- Shared state MUST use typed Pydantic models.
- Use checkpointing for every stage transition.
- Configure retries with bounded exponential backoff.
- Use interrupt for human-in-the-loop approval points.
- Trace runs to LangSmith or LangFuse with mandatory secret redaction.

## MUST Rules
- MUST include input/output hashes for each node execution.
- MUST include rule IDs in mapping and generation node metadata.
- MUST preserve idempotency for retried nodes.
- MUST block deployment path without signed approval artifact.

## Failure Handling
- Deterministic tool failure blocks stage and emits audit error state.
- LLM uncertainty produces UNMAPPED, never guessed mappings.

See human-approval-gate and secret-safe-llm-handling skills for strict gate and redaction patterns.