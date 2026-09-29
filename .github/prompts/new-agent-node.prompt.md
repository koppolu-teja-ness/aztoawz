---
mode: agent
description: Scaffold a new LangGraph node with typed state, tests, tracing, and audit logging.
---
# /new-agent-node

## Inputs
- ${input:agent_name}
- ${input:node_purpose}
- ${input:state_contract_name}

## Procedure
1. Create node skeleton with typed Pydantic input/output models.
2. Add deterministic tool integration points.
3. Add tracing hooks with secret redaction.
4. Add tests for success, retry, and failure paths.
5. Add audit record emission.

## Output Artifact
- node_scaffold_summary.json
  - node_files[]
  - state_models[]
  - test_files[]
  - tracing_enabled
  - audit_enabled

## Stop Condition
Stop when node compiles, tests are present, and audit/tracing are wired.