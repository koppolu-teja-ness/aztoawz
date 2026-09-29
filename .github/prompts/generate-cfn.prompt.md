---
mode: agent
description: Generate CloudFormation YAML from approved mappings and unresolved markers.
---
# /generate-cfn

## Inputs
- ${input:mapping_table_path}
- ${input:template_parameters_path}

## Procedure
1. Transform mapped resources into CloudFormation YAML.
2. Apply tagging, encryption defaults, and stateful resource policies.
3. Preserve trace metadata including rule IDs.
4. Insert explicit placeholders/comments for UNMAPPED items.
5. Emit generation audit record.

## Output Artifact
- template.cfn.yaml
- generation_trace.json
  - resources_generated
  - rule_ids_used[]
  - unmapped_items[]
  - input_hash
  - timestamp

## Stop Condition
Stop when template and generation trace are emitted with full rule coverage.