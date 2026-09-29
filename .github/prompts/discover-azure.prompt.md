---
mode: agent
description: Discover scoped Azure resources and ingest Bicep inputs for migration.
---
# /discover-azure

## Inputs
- ${input:subscription_id}
- ${input:resource_group}
- ${input:bicep_path}

## Procedure
1. Enumerate only Key Vault, Function App, and VNet-related resources in scope.
2. Collect resource metadata and dependencies.
3. Ingest Bicep files and record source hashes.
4. Flag all out-of-scope resources as manual-handling dependencies.
5. Emit discovery audit record.

## Output Artifact
- discovery_report.json
  - scope_summary
  - discovered_resources[]
  - out_of_scope_dependencies[]
  - bicep_sources[]
  - input_hash
  - timestamp

## Stop Condition
Stop when all scoped resources are enumerated and out-of-scope items are flagged.