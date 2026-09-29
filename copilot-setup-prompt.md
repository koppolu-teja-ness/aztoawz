# ROLE
You are a senior platform engineer and GitHub Copilot customization architect. Set up the complete Copilot customization layer for this repository: custom instructions, path-specific instructions, prompt files (workflows), custom agents, and skills.

# PROJECT CONTEXT
Project: **Agentic AI–Powered Azure-to-AWS Infrastructure Migration Assistant** (Bicep → AWS CloudFormation).
Scope is limited to three Azure services:
- Key Vault → Secrets Manager / KMS / ACM + scoped IAM
- Functions → Lambda + API Gateway / EventBridge / S3 events / SQS + IAM execution role
- Virtual Network → VPC, subnets, Security Groups + NACLs, route tables, peering, VPC endpoints / PrivateLink

Everything else is out of scope and must be flagged as a "manual-handling dependency", never auto-migrated.

Pipeline (LangGraph multi-agent):
Discover → Parse & Analyse → Map (RAG) → Generate CFN → Lint / Static-Validate → Plan & Risk-Score → HUMAN APPROVAL → Deploy → Post-Deploy Validate → Test → Report

Agents: Discovery, Parser/Analyzer, Mapping (RAG), CFN Generator, Static Validation, Planning & Risk-Scoring, Human Approval Gate, Deployment, Post-Deployment Validation, Reporting.

Stack: Python 3.11+, LangChain, LangGraph, AWS Bedrock (or Azure OpenAI), PGVector/PostgreSQL, FastAPI, Streamlit or React, boto3, Azure SDK/CLI, cfn-lint, checkov/cfn_nag, Docker, GitHub Actions, LangSmith/LangFuse, CloudWatch.

# NON-NEGOTIABLE RULES (bake into every file you generate)
1. **Secret values never enter LLM context, prompts, logs, traces, fixtures, or test snapshots.** Only metadata (names, versions, tags, access scopes) is handled by agents. Values move via direct SDK calls outside the prompt path. Add a redaction layer and a test for it.
2. **RAG-grounded mapping only.** Every Azure→AWS mapping must cite a knowledge-base rule ID. If no rule matches, emit `UNMAPPED / requires manual intervention`. Never guess or silently drop a construct.
3. **No CloudFormation property is emitted without passing `cfn-lint`**. Never invent properties or resource types.
4. **Least privilege.** No `Action: "*"` or `Resource: "*"` unless a rule explicitly justifies it and the item is marked High Risk. Azure RBAC and access policies map to scoped IAM policies attached to specific principals.
5. **No deployment without an explicit, recorded human approval** (Approve / Reject / Modify). The Deployment Agent must refuse to run without a signed approval artifact tied to the plan hash.
6. **Security-posture diff:** nothing may end up newly public, and IAM must be no broader than the source.
7. **Deterministic tools are the source of truth** for validation (cfn-lint, checkov, boto3 describe calls). The LLM proposes; tools verify.
8. **Every stage is idempotent, checkpointed, and writes an audit record**: inputs hash, outputs, rule IDs used, reviewer, timestamp.
9. Out-of-scope resources are flagged, not migrated.
10. Prefer typed Pydantic models for all state passed between LangGraph nodes.

# DELIVERABLES: create these files

## 1. `.github/copilot-instructions.md` (repo-wide, concise, under ~200 lines)
Include:
- Project purpose and scope table.
- Architecture summary (agents, workflow, state model).
- Tech stack and repo layout (propose the layout: `src/agents/`, `src/graph/`, `src/kb/`, `src/tools/`, `src/api/`, `src/ui/`, `tests/`, `infra/`, `docs/`, `config/`).
- Coding standards: type hints, Pydantic models, ruff + black + mypy, pytest, structured logging, no print.
- Build/test/lint commands (`make`, `pytest`, `cfn-lint`, `checkov`).
- The non-negotiable rules above, worded as MUST / MUST NOT.
- Definition of Done for any PR: tests pass, lint clean, no secrets, audit record emitted, docs updated.
- Instruction to always state which KB rule ID justified a mapping.

## 2. `.github/instructions/*.instructions.md` (path-scoped, each with `applyTo` frontmatter)
- `bicep-parsing.instructions.md` (`src/agents/parser/**`): use `az bicep build` → ARM JSON, resolve parameters/variables/`dependsOn`, build a resource graph, detect unsupported constructs.
- `cloudformation.instructions.md` (`**/*.cfn.yaml`, `infra/cfn/**`, `src/agents/generator/**`): YAML only, `AWSTemplateFormatVersion`, Parameters/Mappings/Conditions conventions, tagging standard, `DeletionPolicy`/`UpdateReplacePolicy` on stateful resources, KMS encryption defaults, no hardcoded ARNs/account IDs, cross-stack exports naming.
- `iam-security.instructions.md`: least-privilege patterns, trust policies for managed-identity mapping, condition keys, permission boundary usage, checkov rules to satisfy.
- `rag-kb.instructions.md` (`src/kb/**`, `kb/**`): rule schema (`rule_id`, source construct, target construct, property map, caveats, examples, confidence, last_verified), chunking strategy, embedding and PGVector conventions, retrieval must return rule IDs, how to add a new rule from the migration issue log.
- `langgraph-agents.instructions.md` (`src/agents/**`, `src/graph/**`): one node per agent, typed state, checkpointer, retries with backoff, human-in-the-loop `interrupt`, tracing to LangSmith/LangFuse with secret redaction.
- `testing.instructions.md` (`tests/**`): pytest, moto for AWS mocks, golden-file tests for Bicep→CFN, property-based tests for mapping, a mandatory "no secret value in logs/prompts" test, cfn-lint as a test.
- `api-ui.instructions.md` (`src/api/**`, `src/ui/**`): FastAPI patterns, approval endpoints, auth, and the approval-gate UI showing counts of Auto-Migratable / Requires Review / High Risk plus the Approve / Reject / Modify actions.
- `cicd-docker.instructions.md` (`.github/workflows/**`, `Dockerfile*`): pipeline stages, OIDC to AWS (no long-lived keys), least-privilege deploy role, required status checks.

## 3. `.github/prompts/*.prompt.md` (workflows; reusable slash-commands with frontmatter `mode: agent` and a `description`)
One per pipeline stage, each with clear inputs (`${input:...}`), step-by-step procedure, output artifact format, and a stop condition:
- `/discover-azure` – enumerate Key Vault / Function App / VNet resources and ingest Bicep.
- `/parse-bicep` – produce the resource graph and a list of unsupported constructs.
- `/map-resources` – RAG lookup, mapping table with rule IDs and confidence, and UNMAPPED list.
- `/generate-cfn` – emit CloudFormation YAML from the mapping.
- `/validate-static` – run cfn-lint and checkov, summarize findings, and propose fixes without weakening security.
- `/plan-and-score` – build the migration plan with sequencing and risk scoring (Auto / Review / High Risk) and the approval-gate summary.
- `/deploy-approved` – verify approval artifact, deploy in dependency order, per-resource status tracking, and a rollback procedure.
- `/validate-deployed` – structural and functional equivalence checks, smoke tests, security-posture diff, and the validation report table.
- `/generate-reports` – migration plan, risk, execution, and validation reports.
- `/add-kb-rule` – add and test a new mapping rule from a discovered edge case.
- `/new-agent-node` – scaffold a LangGraph node with typed state, tests, tracing, and audit logging.
- `/review-security` – PR-level security review against the non-negotiable rules.
- `/migrate-e2e` – orchestrate the full workflow, pausing at the approval gate.

## 4. `.github/agents/*.agent.md` (custom agents / chat modes, each with `description`, `tools`, and a focused system prompt)
- `migration-architect` – design and review architecture, read-only tools.
- `bicep-analyst` – parsing and semantic analysis.
- `cfn-engineer` – generates and fixes CloudFormation with lint feedback.
- `iam-security-reviewer` – read-only, blocks over-permissive policies.
- `validation-engineer` – post-deploy checks and test authoring.
- `rag-curator` – maintains the knowledge base and rule quality.
Each agent must state what it may not do (e.g., the security reviewer cannot edit code; nobody except the Deployment workflow may deploy).

## 5. Skills: `.github/skills/<skill-name>/SKILL.md` (each with frontmatter `name` and `description`, plus supporting files such as templates, scripts, and reference tables)
- `azure-keyvault-to-aws` – access policy/RBAC → IAM, soft-delete → recovery window, secrets/keys/certs → Secrets Manager/KMS/ACM, with mapping table and examples.
- `azure-functions-to-lambda` – runtime mapping, triggers/bindings → API Gateway/EventBridge/S3/SQS, app settings → env vars, managed identity → execution role.
- `azure-vnet-to-vpc` – VNet/subnet/NSG/route table/peering/private endpoint mapping, NSG → SG + NACL split, service endpoints → VPC endpoints, and flagging ExpressRoute/on-prem peering as manual.
- `azure-rbac-to-iam` – role → policy mapping rules and least-privilege guardrails.
- `bicep-to-arm-parsing` – commands, pitfalls, and dependency resolution.
- `cfn-authoring-and-lint` – templates, tagging standard, cfn-lint/checkov usage and common fixes.
- `human-approval-gate` – approval artifact schema, plan hashing, and UI/API contract.
- `post-deploy-validation` – equivalence checks, smoke-test recipes, and security-posture diff.
- `secret-safe-llm-handling` – redaction patterns, what may/may not enter prompts, and tests.
- `migration-reporting` – report templates (plan, risk, execution, validation) matching the examples in the project brief.
Every skill's `description` must clearly state *when* to use it so Copilot can auto-select it.

## 6. Supporting scaffolding
- `config/migration.config.yaml` template (naming conventions, tagging standard, Azure→AWS region map, risk thresholds).
- `docs/copilot-customization.md` explaining each file, how to invoke each prompt/agent/skill, and a recommended end-to-end usage flow.
- `Makefile` targets referenced in the instructions (`lint`, `test`, `validate-cfn`, `scan`, `run-graph`).

# PROCESS
1. First, inspect the repo and summarize what exists. If it's empty, propose the layout and get my confirmation.
2. Generate files in the order above, committing logically (one commit per numbered group).
3. Keep each instruction file focused and non-redundant; link to skills instead of duplicating content.
4. Use imperative, testable wording ("MUST", "MUST NOT") and give short good/bad examples where rules are subtle.
5. After generation, print a checklist table of all files created, and list any assumptions or open questions.
6. Do NOT implement application code in this task. Only the Copilot customization layer and scaffolding.
