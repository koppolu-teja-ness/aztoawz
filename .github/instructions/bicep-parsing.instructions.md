---
applyTo: src/agents/parser/**
---
# Bicep Parsing Instructions

## Objective
Parse Bicep deterministically and produce a complete dependency-aware model for migration decisions.

## Required Process
1. Run az bicep build to compile Bicep into ARM JSON before semantic parsing.
2. Resolve parameters, variables, modules, and dependsOn relationships.
3. Build a directed resource graph with node type, name, scope, and dependencies.
4. Detect unsupported constructs and emit explicit manual-handling dependency records.
5. Persist parsing artifacts and hashes for idempotent re-runs.

## MUST Rules
- MUST treat ARM JSON as canonical parse input after compilation.
- MUST preserve source-to-compiled traceability for each resource node.
- MUST emit unsupported constructs with reason, location, and impact.
- MUST NOT infer missing values when resolution fails; classify as unresolved.
- MUST include deterministic parse errors in audit records.

## Output Contract
- Parsed graph object (typed model)
- Unsupported constructs list
- Parse diagnostics
- Stage audit record

## Quality Checks
- Graph is acyclic or reports cycle diagnostics.
- Every parsed resource has an origin reference.
- Unsupported list is non-empty when unsupported features are present.

See the bicep-to-arm-parsing skill for command details and edge-case handling.