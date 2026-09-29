# Commands and Pitfalls

## Core Command
- az bicep build --file <path>

## Pitfalls
- Implicit dependencies hidden in expressions.
- Module output chains with missing references.
- Parameter files not aligned with template version.

## Expected Output
- ARM JSON
- Parse diagnostics
- Resource dependency graph