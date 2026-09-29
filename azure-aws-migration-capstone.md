# Agentic AI–Powered Azure-to-AWS Infrastructure Migration Assistant

### (Bicep → AWS CloudFormation: Key Vault, Functions & Virtual Network)

---

## 1. Business Scenario

Enterprises increasingly adopt multi-cloud or cloud-exit strategies — moving workloads from Azure to AWS — driven by licensing cost pressure, mergers and acquisitions, vendor-lock-in avoidance, contractual requirements from customers, or a desire to consolidate onto a single cloud for operational simplicity.

Unlike application code, infrastructure defined as code (IaC) does not "just run" on another cloud. A team that has built its Azure footprint using **Bicep** must manually re-author every resource definition in **AWS CloudFormation**, re-map identity and access constructs (Azure RBAC / Managed Identity → AWS IAM), re-model networking primitives (VNet/NSG → VPC/Security Groups), and then manually verify that the new environment behaves the same as the old one. This is slow, repetitive, and error-prone — a single missed access policy or an incorrectly scoped security group can create a serious security gap.

## 2. Problem Statement

There is no reliable, automated way to take an Azure environment defined in Bicep and produce a **validated, deployable, functionally-equivalent** AWS CloudFormation implementation. Existing options are:

- **Fully manual rewriting** — slow, inconsistent across engineers, and hard to audit.
- **Generic/naïve IaC "converters"** — attempt a syntactic, resource-by-resource translation without understanding cross-cloud semantic equivalence (e.g., they don't know that an Azure Key Vault access policy needs to become a scoped IAM policy attached to specific principals, not a wildcard).
- **No built-in validation** — most tools stop at "here is some YAML," with no verification that the deployed AWS resources are structurally and functionally equivalent to the source.

For a defined, high-value slice of the estate — **Key Vault, Functions, and Virtual Network** — this capstone builds an agentic system that closes this gap end-to-end: from discovery through deployment through verification.

## 3. Business Use Case

**Who benefits:**

- Organizations executing a planned Azure → AWS migration (cost optimization, M&A cloud consolidation, contractual/regulatory requirements, avoiding single-vendor lock-in).
- Platform/DevOps teams who need a repeatable, auditable migration process instead of a one-off manual effort.
- Security teams who need assurance that identity, secrets, and network posture are preserved (or improved) after migration — not silently weakened.

**Value delivered:**

- Reduces migration effort for the covered services from days/weeks of manual rewriting to a review-and-approve workflow.
- Produces an audit trail: what was discovered, what was mapped, what a human approved, what was deployed, and what was validated.
- Reduces the risk of security misconfiguration (over-permissive IAM, unintentionally public Function endpoints, misconfigured subnets/routes) through deterministic pre- and post-deployment checks.
- Gives migration leads a defensible go/no-go decision point instead of "it looked right in testing."

## 4. Capstone Objective

Build an **Agentic AI–powered migration assistant** that can, for Azure Key Vault, Azure Functions, and Azure Virtual Network resources:

1. Discover the source Azure resources and their Bicep definitions.
2. Semantically analyze the Bicep templates and identify AWS-incompatible constructs.
3. Map each Azure construct to its AWS equivalent using an LLM + RAG-grounded knowledge base (not free-form LLM guessing).
4. Generate valid, deployable AWS CloudFormation YAML.
5. Statically validate the generated templates (lint, security policy scan).
6. Produce a migration plan with risk scoring and route it through a **human-in-the-loop approval gate**.
7. Deploy the approved templates to AWS.
8. Validate that the deployed AWS environment is structurally and functionally equivalent to the Azure source.
9. Produce structured migration and validation reports.

## 5. Scope

| Azure Resource      | Bicep Construct(s)                                                                                       | Target AWS Service(s)                                                                            | Notes                                                                   |
| ------------------- | -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------- |
| **Key Vault**       | `Microsoft.KeyVault/vaults`, secrets, keys, certificates, access policies                                | AWS Secrets Manager (secrets), AWS KMS (keys), AWS Certificate Manager (certs), IAM policies     | Access-policy/RBAC → scoped IAM policy; soft-delete → recovery window   |
| **Functions**       | `Microsoft.Web/sites` (Function App), HTTP/Timer/Blob/Queue/Service Bus triggers, bindings, app settings | AWS Lambda + API Gateway (HTTP), EventBridge (Timer), S3 event notifications (Blob), SQS (Queue) | Runtime mapping, env-var mapping, managed identity → IAM execution role |
| **Virtual Network** | `Microsoft.Network/virtualNetworks`, subnets, NSGs, route tables, peerings, private endpoints            | VPC, subnets, Security Groups + NACLs, route tables, VPC peering, VPC endpoints / PrivateLink    | Service endpoints → VPC endpoints; NSG rules → SG + NACL split          |

**Out of scope:** all other Azure services (App Service, SQL Database, Storage Accounts, Cosmos DB, etc.), application-code changes, data-plane migration of secret _values_ through the LLM context, and bidirectional/continuous sync.

## 6. Why This Is Different From Existing Approaches

| Existing Approach                                                       | Limitation                                                                                     | This Capstone                                                                            |
| ----------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Manual rewrite by engineers                                             | Slow, inconsistent, hard to audit                                                              | Automated, repeatable, produces an audit trail                                           |
| Generic IaC "transpilers"                                               | Syntax-level translation only; no semantic cross-cloud equivalence (identity, network posture) | RAG-grounded mapping of _semantics_, not just resource names                             |
| Single-shot LLM prompting ("convert this Bicep to CloudFormation")      | Prone to hallucinated properties, no grounding in real mapping rules, no validation            | Multi-agent pipeline with a retrieval-grounded knowledge base + deterministic validation |
| Commercial cloud-migration tools (e.g., server/VM lift-and-shift tools) | Focused on compute/workload migration, not declarative IaC-to-IaC transformation               | Purpose-built for IaC transformation of security- and network-sensitive resources        |
| Ad-hoc scripts                                                          | Stop once YAML is produced; no proof the deployed result matches the source                    | End-to-end: discover → transform → **deploy** → **verify** functional equivalence        |

The core differentiator is that this system treats migration as a **governed, verifiable workflow** — combining LLM reasoning for translation with deterministic tooling (linting, policy scanning, live resource comparison) and a mandatory human checkpoint before touching security- or network-critical infrastructure.

## 7. Assumptions

- Source Bicep templates are available, or can be produced from a live Azure environment (e.g., via `az bicep decompile` / ARM template export).
- An AWS account and baseline IAM permissions for the deployment agent exist; the _target_ resources themselves do not yet exist.
- Resources outside the three in-scope services that are referenced by Bicep templates will be **flagged as dependencies requiring manual handling**, not auto-migrated.
- Migration is one-directional (Azure → AWS) for this capstone; rollback means re-deploying the Azure source, not live bidirectional sync.
- Naming conventions, tagging standards, and Azure-region → AWS-region mapping are defined up front in a configuration file.
- Secret **values** (as opposed to secret metadata/structure) are never passed through the LLM context; value copying happens through a direct, secure SDK/API call outside the agent's prompt path.
- A human reviewer with migration authority is available to action the approval gate (approve / reject / modify).

## 8. Implementation Approach

### 8.1 Multi-Agent Architecture (LangGraph)

| Agent                            | Responsibility                                                                                                           |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Discovery Agent                  | Enumerates Azure Key Vault, Function App, and VNet resources (via Azure SDK/CLI) and/or ingests existing Bicep files     |
| Parser/Analyzer Agent            | Parses Bicep (via `az bicep build` → ARM JSON, or AST parsing) and extracts resource graph, dependencies, and properties |
| Mapping Agent (RAG)              | Retrieves relevant mapping rules from the knowledge base and proposes the AWS-equivalent resource/property set           |
| CFN Generator Agent              | Produces syntactically valid CloudFormation YAML from the mapped resource graph                                          |
| Static Validation Agent          | Runs `cfn-lint`, `checkov`/`cfn_nag` policy scans, and schema validation on generated templates                          |
| Planning & Risk-Scoring Agent    | Builds the migration plan: sequencing, dependencies, auto-migratable vs. needs-review vs. high-risk objects              |
| Human Approval Gate              | Presents the plan for Approve / Reject / Modify before any deployment                                                    |
| Deployment Agent                 | Deploys approved templates via `boto3`/CloudFormation, in dependency order                                               |
| Post-Deployment Validation Agent | Compares live AWS resource state against the Azure source (structural + functional)                                      |
| Reporting Agent                  | Produces migration plan, execution, and validation reports                                                               |

### 8.1.1 Current Implementation Status (as of 2026-09-25)

The architecture above is the target design. The current codebase already
implements the full node wiring in LangGraph, with required LLM usage for
selected reasoning/reporting nodes.

| Agent                            | RAG usage | LLM usage | Current behavior                                                            |
| -------------------------------- | --------- | --------- | --------------------------------------------------------------------------- |
| Discovery Agent                  | No        | No        | Deterministic                                                               |
| Parser/Analyzer Agent            | No        | No        | Deterministic                                                               |
| Mapping Agent                    | Yes       | Required  | Rule-based mapping baseline with required Bedrock refinement                |
| CFN Generator Agent              | No        | No        | Deterministic                                                               |
| Static Validation Agent          | No        | No        | Deterministic                                                               |
| Planning & Risk-Scoring Agent    | No        | Required  | Deterministic risk scoring baseline with required Bedrock reason enrichment |
| Human Approval Gate              | No        | No        | Deterministic control-flow gate                                             |
| Deployment Agent                 | No        | No        | Deterministic                                                               |
| Post-Deployment Validation Agent | No        | No        | Deterministic                                                               |
| Reporting Agent                  | No        | Required  | Required Bedrock summary generation                                         |

LLM calls are enabled only when configuration specifies all of:

- `llm.enabled = true`
- `llm.provider = aws_bedrock`
- `llm.bedrock.model_id` is non-empty

If these conditions are not met, LLM-required agents fail fast and the
workflow does not proceed through those steps.

For implementation-level details and source-of-truth behavior, see
`docs/agent_responsibilities.md` and `ARCHITECTURE.md`.

### 8.2 End-to-End Workflow

```
Discover → Parse & Analyse → Map (RAG) → Generate CFN → Lint / Static-Validate
     → Plan & Risk-Score → HUMAN APPROVAL → Deploy → Post-Deploy Validate
     → Test → Report
```

```mermaid
flowchart TD
  discovery[discovery] --> parse_analyze[parse_analyze]
  parse_analyze --> map[map]
  map --> generate_cfn[generate_cfn]
  generate_cfn --> static_validate[static_validate]
  static_validate --> plan_risk[plan_risk]
  plan_risk --> human_approval{human_approval}

  human_approval -->|approved| deploy[deploy]
  human_approval -->|modified| map
  human_approval -->|pending| end_pending([END])
  human_approval -->|rejected| end_rejected([END])

  deploy --> post_deploy_validate[post_deploy_validate]
  post_deploy_validate --> report[report]
  report --> end_success([END])
```

### 8.3 Human Approval Gate (example)

```
Migration Plan
───────────────
Key Vault objects   : 22   (secrets: 18, keys: 4)
Function Apps        : 6   (32 functions across triggers)
VNets                : 2   (9 subnets, 47 NSG rules)

Auto-Migratable      : 268
Requires Review       : 34   (e.g. wildcard access-policy scopes)
High Risk Objects     : 6    (e.g. public-facing Function, VNet peering
                               to on-prem with no direct AWS equivalent)

        ↓

    HUMAN REVIEW

  [ Approve ]  [ Reject ]  [ Modify ]
```

### 8.4 Validation Report (example)

```
Migration Validation Report
--------------------------------------------------------
Resource Type          Source (Azure)   Target (AWS)   Status
Key Vault Secrets       18               18             PASS
Key Vault Keys           4                4             PASS
Function Apps / Lambdas  6 (32 funcs)     6 (32 fns)     PASS
VNets / VPCs             2                2              PASS
Subnets                  9                9              PASS
NSG Rules → SG+NACL      47               47             PASS
--------------------------------------------------------
Overall Status: PASS (2 warnings — see detailed report)
```

## 9. Validation & Testing Strategy

**Pre-deployment (static):**

- `cfn-lint` schema/syntax validation
- `checkov` / `cfn_nag` security-policy scanning (public exposure, overly-broad IAM, unencrypted resources)

**Post-deployment (live):**

- Resource-count and property equivalence (secret/key counts, Lambda runtime/env-vars/trigger wiring vs. Function bindings, VPC CIDR/subnet/route-table parity)
- Functional smoke tests (invoke each migrated Lambda endpoint, retrieve each secret via the AWS SDK, verify network reachability matching the original subnet placement)
- Security-posture diff (nothing newly public, IAM scoped no more broadly than the source RBAC/access policy)
- Structured test report identifying schema issues, mapping gaps, referential/dependency problems, and missing objects

## 10. Suggested Knowledge Base (RAG)

- Bicep resource type → CloudFormation resource type mapping tables
- Property-level mapping per resource type (e.g., Key Vault access policy → IAM policy statement patterns)
- Azure RBAC role → IAM policy mapping rules
- Azure Managed Identity → IAM role / trust-policy patterns
- Trigger mapping rules (HTTP/Timer/Blob/Queue → API Gateway/EventBridge/S3/SQS)
- Known incompatibilities and documented workarounds (e.g., Key Vault soft-delete vs. Secrets Manager recovery window; VNet service endpoints vs. VPC endpoints)
- Azure region ↔ AWS region mapping
- Naming and tagging translation conventions
- Historical migration issue log (grows as the agent encounters new edge cases)
- AWS security best practices for KMS, Secrets Manager, Lambda, and VPC

## 11. Suggested Technology Stack

**AI / Agentic AI:** Python, AWS Bedrock (or Azure OpenAI for the LLM layer), LangChain, LangGraph, Prompt Engineering, RAG, Embeddings, PGVector

**Cloud Tooling:** Azure CLI/SDK (discovery + Bicep build/decompile), `boto3`, AWS CLI, `cfn-lint`, `checkov`/`cfn_nag`

**Application:** FastAPI, PostgreSQL/PGVector, Streamlit or React dashboard

**DevOps:** Docker, GitHub Actions (CI/CD), optional Terraform for AWS account bootstrap

**Observability:** LangSmith / LangFuse (agent tracing), AWS CloudWatch, Prometheus/Grafana

## 12. Expected Deliverables

1. Working agentic migration assistant (Bicep → CloudFormation)
2. Azure discovery module (Key Vault, Functions, VNet)
3. Bicep parsing & semantic analysis engine
4. RAG-based Azure→AWS migration knowledge base
5. Resource & property mapping engine
6. CloudFormation YAML generation engine
7. Static validation module (lint + security policy scan)
8. Migration planning & risk-scoring agent
9. Human-in-the-loop approval workflow (approve/reject/modify)
10. Automated AWS deployment pipeline (boto3/CloudFormation)
11. Post-deployment structural & functional validation framework
12. Migration test suite and structured test report
13. Migration risk report
14. Migration execution & validation report
15. FastAPI backend + Streamlit/React dashboard
16. Architecture diagram
17. Source code repository
18. Docker + CI/CD pipeline (GitHub Actions)
19. Observability dashboard (LangSmith + CloudWatch/Grafana)
20. Final demonstration video

## 13. Future Enhancements

- Extend coverage to additional Azure services (Storage Accounts, SQL Database, App Service, Cosmos DB, API Management, Application Gateway).
- Support additional IaC source formats (ARM JSON, Terraform `azurerm`) and target formats (Terraform AWS provider, AWS CDK).
- Continuous drift detection between source and migrated environments post-cutover.
- Cost-comparison and right-sizing recommendations (e.g., Lambda memory tuning, KMS vs. Secrets Manager pricing).
- Automated rollback on validation failure.
- Multi-region / disaster-recovery–aware migration planning.
- Self-improving knowledge base fed by outcomes of past migrations (feedback loop on mapping accuracy).
- Support brownfield migration — importing pre-existing AWS resources into IaC/state for hybrid environments.
- Policy-as-code governance (e.g., OPA/Conftest) integrated directly into the approval gate.

## 14. Key Risks & Mitigations

| Risk                                                                | Mitigation                                                                                                                 |
| ------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| LLM hallucinates a CloudFormation property that doesn't exist       | RAG grounding + mandatory `cfn-lint`/schema validation before any deployment step                                          |
| Over-permissive IAM policy generated from a broad Azure RBAC role   | Least-privilege mapping rules in the knowledge base + `checkov` policy scan flags for review                               |
| Secret values exposed via LLM prompt/context                        | Secret values never enter the LLM context; copied via direct SDK calls only                                                |
| Unsupported construct (e.g., ExpressRoute peering) silently dropped | Discovery/mapping agent explicitly flags unmappable constructs as "requires manual intervention" rather than omitting them |
| Deployment partially succeeds, leaving inconsistent state           | Migration checkpoints + per-resource deployment status tracking, with defined rollback procedure                           |
