---
applyTo: "src/kb/**,kb/**"
---
# RAG Knowledge Base Instructions

## Objective
Maintain a verifiable mapping knowledge base that drives deterministic Azure-to-AWS decisions.

## Rule Schema
Each rule record MUST include:
- rule_id
- source_construct
- target_construct
- property_map
- caveats
- examples
- confidence
- last_verified

## Retrieval Rules
- Retrieval MUST return rule_id values used by decisions.
- If no rule is retrieved with acceptable confidence, output UNMAPPED and manual intervention.
- Mapping agents MUST NOT guess or fabricate rule references.

## Indexing Conventions
- Chunk by semantic unit: one construct mapping per chunk.
- Keep examples and caveats co-located with property maps.
- Store embeddings in PGVector with stable document/version identifiers.

## Rule Lifecycle
1. Capture edge case from migration issue log.
2. Author candidate rule with tests.
3. Validate against golden fixtures.
4. Record last_verified and reviewer.

See add-kb-rule prompt workflow and migration-reporting skill for governance linkage.