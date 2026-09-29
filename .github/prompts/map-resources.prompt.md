---
mode: agent
description: Map Azure constructs to AWS targets using RAG and emit rule-linked decisions.
---
# /map-resources

## Inputs
- ${input:parse_artifact_path}
- ${input:kb_index_name}

## Procedure
1. Retrieve candidate mapping rules from KB.
2. Match each source construct to target construct with rule_id citation.
3. Record confidence per mapping.
4. Emit UNMAPPED / requires manual intervention when no acceptable rule exists.
5. Produce mapping audit record with all rule IDs used.

## Output Artifact
- mapping_table.json
  - mapped_items[]: source, target, rule_id, confidence, caveats
  - unmapped_items[]
  - manual_interventions[]
  - input_hash
  - timestamp

## Stop Condition
Stop when every source construct is either mapped with rule_id or UNMAPPED.