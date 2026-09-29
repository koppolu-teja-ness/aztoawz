---
name: bicep-to-arm-parsing
description: Use when compiling and parsing Bicep into ARM JSON, resolving dependencies, and surfacing unsupported constructs for migration workflows.
---
# Bicep to ARM Parsing Skill

## Use This Skill When
- Bicep sources must be normalized for deterministic analysis.

## Rules
- Run az bicep build before graph extraction.
- Resolve parameters, variables, and dependsOn relationships.
- Report unresolved symbols as explicit diagnostics.
- Emit unsupported constructs for manual handling.

See commands-and-pitfalls.md for commands and failure patterns.