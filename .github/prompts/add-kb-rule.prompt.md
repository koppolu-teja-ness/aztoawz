---
mode: agent
description: Add a new mapping rule from an edge case and validate it against fixtures.
---
# /add-kb-rule

## Inputs
- ${input:edge_case_description}
- ${input:source_construct}
- ${input:target_construct}

## Procedure
1. Create candidate rule with required schema fields.
2. Assign stable rule_id and confidence.
3. Add examples and caveats.
4. Add or update tests and golden fixtures.
5. Re-index KB and verify retrieval returns new rule_id.

## Output Artifact
- kb_rule_change.json
  - rule_id
  - files_changed[]
  - tests_added[]
  - verification_result
  - timestamp

## Stop Condition
Stop when rule retrieval and tests pass with rule_id visibility.