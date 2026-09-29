# Copilot Instructions: Azure-to-AWS Migration Assistant

## Purpose
This repository defines an agentic migration assistant that converts Azure Bicep infrastructure into AWS CloudFormation for a narrow scope.

## Scope
| Azure Service | AWS Target | In Scope |
|---|---|---|
| Key Vault | Secrets Manager, KMS, ACM, scoped IAM | Yes |
| Functions | Lambda, API Gateway, EventBridge, S3 events, SQS, IAM execution role | Yes |
| Virtual Network | VPC, subnets, Security Groups, NACLs, route tables, peering, VPC endpoints/PrivateLink | Yes |
| Any other Azure service | N/A | No (manual-handling dependency) |

Out-of-scope resources MUST be flagged as manual-handling dependencies and MUST NOT be auto-migrated.

## Architecture Summary
Agents:
- Discovery Agent
- Parser/Analyzer Agent
- Mapping Agent (RAG)
- CloudFormation Generator Agent
- Static Validation Agent
- Planning and Risk-Scoring Agent
- Human Approval Gate Agent
- Deployment Agent
- Post-Deployment Validation Agent
- Reporting Agent

Workflow:
1. Discover
2. Parse and Analyse
3. Map with RAG
4. Generate CloudFormation
5. Lint and Static-Validate
6. Plan and Risk-Score
7. Human Approval (Approve/Reject/Modify)
8. Deploy
9. Post-Deploy Validate
10. Test
11. Report

State model requirements:
- Use typed Pydantic models for all LangGraph node inputs/outputs.
- Every stage is idempotent and checkpointed.
- Every stage emits an audit record with input hash, outputs, rule IDs used, reviewer, and timestamp.

## Preferred Repository Layout
- src/agents/
- src/graph/
- src/kb/
- src/tools/
- src/api/
- src/ui/
- tests/
- infra/
- docs/
- config/

## Tech Stack
- Python 3.11+
- LangChain, LangGraph
- AWS Bedrock or Azure OpenAI
- PostgreSQL + PGVector
- FastAPI
- Streamlit or React
- boto3
- Azure SDK/CLI
- cfn-lint
- checkov or cfn_nag
- Docker
- GitHub Actions
- LangSmith or LangFuse
- CloudWatch

## Engineering Standards
- Type hints are required for all Python code.
- Pydantic models are required for graph state contracts.
- Use ruff, black, mypy, and pytest.
- Use structured logging; do not use print statements.
- Keep deterministic tool outputs as source of truth.

## Build, Test, and Validation Commands
- make lint
- make test
- make validate-cfn
- make scan
- make run-graph

## Non-Negotiable Rules
1. Secret values MUST NOT enter LLM context, prompts, logs, traces, fixtures, or test snapshots.
   - Only metadata (name, version, tags, access scope) may be processed by agents.
   - Secret value movement MUST occur via direct SDK calls outside prompt paths.
   - A redaction layer and tests for redaction are required.
2. Mapping MUST be RAG-grounded.
   - Every Azure-to-AWS mapping MUST cite a knowledge base rule ID.
   - If no rule matches, output UNMAPPED / requires manual intervention.
   - Agents MUST NOT guess or silently drop constructs.
3. CloudFormation output MUST pass cfn-lint.
   - Do not emit unsupported or invented properties or resource types.
4. IAM MUST enforce least privilege.
   - Action: "*" and Resource: "*" are prohibited unless explicitly justified by a rule and marked High Risk.
   - Azure RBAC/access policies MUST map to scoped IAM policies bound to specific principals.
5. Deployment MUST require explicit recorded human approval.
   - Deployment Agent MUST refuse execution without a signed approval artifact tied to the plan hash.
6. Security posture MUST NOT regress.
   - Nothing may become newly public.
   - IAM permissions MUST NOT be broader than source intent.
7. Deterministic tools MUST verify outputs.
   - cfn-lint, checkov, and boto3 describe calls are authoritative validators.
8. Every stage MUST be idempotent, checkpointed, and auditable.
9. Out-of-scope resources MUST be flagged and MUST NOT be migrated.
10. LangGraph state contracts MUST use typed Pydantic models.

## Subtle Rule Examples
Good:
- No KB match found for a Bicep property: mark UNMAPPED and include manual action.

Bad:
- Inferring an AWS property without a rule ID.

Good:
- Policy allows specific actions on one ARN set tied to mapped principal.

Bad:
- Wildcard action/resource in default generated policy.

## Pull Request Definition of Done
A PR is complete only when all conditions are met:
- Tests pass.
- Lint/type checks pass.
- No secret values appear in prompts/logs/artifacts.
- CloudFormation validation and security scans pass.
- Audit record emission is implemented for changed stages.
- Documentation is updated.
- Every mapping change states the KB rule ID that justified it.

## Mapping Output Rule
When describing or generating a mapping, always state the KB rule ID that justified each mapping row.