---
mode: agent
description: Compile and parse Bicep into a dependency graph and unsupported construct list.
---
# /parse-bicep

## Inputs
- ${input:bicep_path}
- ${input:discovery_report_path}

## Procedure
1. Compile Bicep with az bicep build.
2. Parse ARM JSON and resolve parameters, variables, and dependsOn.
3. Build typed resource graph.
4. Detect unsupported constructs.
5. Emit parse diagnostics and audit record.

## Output Artifact
- parse_artifact.json
  - resource_graph
  - unresolved_values[]
  - unsupported_constructs[]
  - parse_diagnostics[]
  - input_hash
  - timestamp

## Stop Condition
Stop when resource graph is complete and unsupported constructs are explicitly listed.