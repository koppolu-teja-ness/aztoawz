# Azure to AWS Migration Assistant

## Project Overview

This repository implements an agentic pipeline that migrates a scoped subset of Azure infrastructure definitions (Bicep/ARM-derived) into AWS CloudFormation.

In-scope source services:
- Azure Key Vault
- Azure Functions
- Azure Virtual Network

Out-of-scope constructs are never auto-migrated. They are explicitly marked as UNMAPPED and carried forward as manual-handling dependencies.

Core properties of the system:
- Typed state contracts with Pydantic models
- Deterministic stage outputs with per-stage audit records
- RAG-grounded mapping decisions using KB rule IDs
- Human approval gate before deployment
- Static and post-deploy validation stages

## Repository Layout

- src/agents: workflow stages (discovery, parser, mapping, generator, validator, planner, approval, deployer, postvalidate, reporter)
- src/graph: LangGraph assembly and shared state contracts
- src/kb: knowledge base schema, ingestion, retrieval
- src/api: FastAPI endpoints for migration lifecycle
- src/ui: Streamlit UI scaffold
- kb/rules: YAML rule definitions with canonical rule_id entries
- infra/cfn: CloudFormation output directory
- tests: unit and integration coverage

## Run Locally

### Prerequisites

- Python 3.11+
- Docker and Docker Compose
- cfn-lint and checkov available in your environment for full validation targets

### Python Setup

Install dependencies:

    python -m pip install --upgrade pip
    python -m pip install -e .[dev]

### Make Targets

The project Makefile includes these primary targets:
- make install
- make lint
- make format
- make typecheck
- make test
- make validate-cfn
- make validate-kb
- make scan
- make run-graph
- make run-api
- make run-ui

Typical local verification flow:

    make lint
    make typecheck
    make test
    make validate-kb
    make validate-cfn

### Docker Compose

Start app plus pgvector:

    docker compose up --build

Services:
- app: FastAPI app exposed on port 8000
- pgvector: PostgreSQL with pgvector exposed on port 5432

Stop and remove:

    docker compose down

## KB and rule_id Model

Rules live under kb/rules and are validated against src/kb/schema.py.

Each rule includes:
- rule_id
- source_service and source_construct
- target_service and target_construct
- property_map
- caveats
- examples
- confidence
- last_verified

How rule IDs are used:
- Retrieval returns top candidates with rule_id in src/kb/retriever.py
- Mapping stage records rule_id per mapped resource in MappingResult
- Mapping audit record stores rule_ids_used
- Generator stage carries rule_ids_used into each generated CFN artifact
- Planner/reporter stages include rule references in outputs and reports

If no candidate clears the confidence threshold, mapping returns UNMAPPED and the resource is marked for manual intervention.

## Approval Gate

Approval is an explicit stage between planning and deployment.

Behavior summary:
- Planner computes a deterministic plan_hash
- Approval gate persists pending plan metadata
- Graph execution interrupts until a human decision record exists
- Resume is allowed only when decision plan_hash matches current plan_hash
- Deployment is permitted only for APPROVE decisions

Checkpoint files are persisted by the file-backed approval store:
- pending_plan.json
- approval_record.json

## Adding a New Mapping Rule

1. Create a new file in kb/rules, for example kv-008-rotation.yaml.
2. Include all required schema fields from src/kb/schema.py.
3. Use a unique, stable rule_id.
4. Add at least one realistic example and caveats.
5. Set confidence and last_verified.
6. Run KB validation:

       python scripts/validate_kb.py

7. Add or update tests to assert the new rule_id appears in mapping outcomes.
8. If retrieval/index workflows are used, re-ingest KB rules into pgvector.

Authoring rule guidance:
- Keep one construct mapping per rule file
- Do not guess mappings; use UNMAPPED for unsupported cases
- Ensure caveats clearly describe non-1:1 behavior differences
