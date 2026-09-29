# Copilot Customization Guide

## Purpose
This repository contains a complete GitHub Copilot customization layer for an agentic Azure-to-AWS migration assistant focused on Key Vault, Functions, and Virtual Network migrations.

## What Was Added

### Repo-wide behavior
- .github/copilot-instructions.md

### Path-scoped instructions
- .github/instructions/bicep-parsing.instructions.md
- .github/instructions/cloudformation.instructions.md
- .github/instructions/iam-security.instructions.md
- .github/instructions/rag-kb.instructions.md
- .github/instructions/langgraph-agents.instructions.md
- .github/instructions/testing.instructions.md
- .github/instructions/api-ui.instructions.md
- .github/instructions/cicd-docker.instructions.md

### Prompt workflows
- .github/prompts/discover-azure.prompt.md
- .github/prompts/parse-bicep.prompt.md
- .github/prompts/map-resources.prompt.md
- .github/prompts/generate-cfn.prompt.md
- .github/prompts/validate-static.prompt.md
- .github/prompts/plan-and-score.prompt.md
- .github/prompts/deploy-approved.prompt.md
- .github/prompts/validate-deployed.prompt.md
- .github/prompts/generate-reports.prompt.md
- .github/prompts/add-kb-rule.prompt.md
- .github/prompts/new-agent-node.prompt.md
- .github/prompts/review-security.prompt.md
- .github/prompts/migrate-e2e.prompt.md

### Custom agents
- .github/agents/migration-architect.agent.md
- .github/agents/bicep-analyst.agent.md
- .github/agents/cfn-engineer.agent.md
- .github/agents/iam-security-reviewer.agent.md
- .github/agents/validation-engineer.agent.md
- .github/agents/rag-curator.agent.md

### Skills
- .github/skills/azure-keyvault-to-aws/
- .github/skills/azure-functions-to-lambda/
- .github/skills/azure-vnet-to-vpc/
- .github/skills/azure-rbac-to-iam/
- .github/skills/bicep-to-arm-parsing/
- .github/skills/cfn-authoring-and-lint/
- .github/skills/human-approval-gate/
- .github/skills/post-deploy-validation/
- .github/skills/secret-safe-llm-handling/
- .github/skills/migration-reporting/

### Supporting scaffolding
- config/migration.config.yaml
- Makefile
- docs/copilot-customization.md

## How To Invoke Prompts
Use slash prompts by name in Copilot Chat:
- /discover-azure
- /parse-bicep
- /map-resources
- /generate-cfn
- /validate-static
- /plan-and-score
- /deploy-approved
- /validate-deployed
- /generate-reports
- /add-kb-rule
- /new-agent-node
- /review-security
- /migrate-e2e

Each prompt defines:
- required inputs
- procedure
- output artifact format
- stop condition

## How To Use Agents
Select the agent mode based on task focus:
- migration-architect: architecture and contract reviews (read-only)
- bicep-analyst: parsing and semantic analysis
- cfn-engineer: CloudFormation generation and lint-driven fixes
- iam-security-reviewer: least-privilege and posture checks (read-only)
- validation-engineer: validation and test authoring
- rag-curator: KB rule maintenance and retrieval quality

## How Skills Are Auto-Selected
Each skill has a when-to-use description in its SKILL.md frontmatter. Keep descriptions specific and action-oriented so Copilot can route correctly.

## Recommended End-to-End Usage Flow
1. Run /discover-azure with subscription, RG, and Bicep path.
2. Run /parse-bicep to produce resource graph and unsupported list.
3. Run /map-resources to get rule-backed mappings and UNMAPPED items.
4. Run /generate-cfn to emit CloudFormation YAML and generation trace.
5. Run /validate-static for cfn-lint/checkov findings and secure remediations.
6. Run /plan-and-score for execution sequencing and risk classes.
7. Perform approval action (Approve/Reject/Modify) through API/UI gate.
8. Run /deploy-approved only with signed matching approval artifact.
9. Run /validate-deployed for equivalence and security-posture diff.
10. Run /generate-reports for final plan/risk/execution/validation outputs.

## Maintenance Guidance
- Add new mappings via /add-kb-rule only after edge-case evidence.
- Keep all mapping decisions tied to rule IDs.
- Fail closed on unknowns: emit UNMAPPED, not guessed mappings.
- Keep tests and reports secret-safe and auditable.