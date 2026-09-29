"""Typed state contracts for the LangGraph migration pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PlanItemStatus(str, Enum):
    """Execution status used by planning and risk scoring."""

    AUTO = "AUTO"
    REVIEW = "REVIEW"
    HIGH_RISK = "HIGH_RISK"


class ApprovalDecision(str, Enum):
    """Human approval outcomes for the deployment gate."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    MODIFY = "MODIFY"


class FindingSeverity(str, Enum):
    """Severity levels for validation and post-deployment findings."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class DiscoveredResource(BaseModel):
    """Resource discovered from source Azure inventory."""

    model_config = ConfigDict(extra="forbid")

    resource_id: str
    resource_type: str
    name: str
    resource_group: str | None = None
    region: str
    tags: dict[str, str] = Field(default_factory=dict)
    raw_properties: dict[str, Any] = Field(default_factory=dict)


class ParsedResource(BaseModel):
    """Normalized representation after parser/analyzer stage."""

    model_config = ConfigDict(extra="forbid")

    resource_id: str
    source_type: str
    normalized_type: str
    properties: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)


class ResourceGraphNode(BaseModel):
    """Node metadata for parsed resource dependency graph."""

    model_config = ConfigDict(extra="forbid")

    resource_id: str
    source_type: str
    name: str
    scope: str
    origin_ref: str


class ResourceGraphEdge(BaseModel):
    """Directed edge from dependency resource to dependent resource."""

    model_config = ConfigDict(extra="forbid")

    from_resource_id: str
    to_resource_id: str
    reason: str = "dependsOn"


class ResourceDependencyGraph(BaseModel):
    """Typed graph emitted by parser/analyzer stage."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[ResourceGraphNode] = Field(default_factory=list)
    edges: list[ResourceGraphEdge] = Field(default_factory=list)


class MappingResult(BaseModel):
    """RAG-grounded mapping decision from Azure construct to AWS construct."""

    model_config = ConfigDict(extra="forbid")

    source_resource_id: str
    target_resource_type: str
    mapped: bool
    rule_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    caveats: list[str] = Field(default_factory=list)


class CfnArtifact(BaseModel):
    """CloudFormation resource snippet generated for one mapping output."""

    model_config = ConfigDict(extra="forbid")

    logical_id: str
    resource_type: str
    properties: dict[str, Any] = Field(default_factory=dict)
    rule_ids_used: list[str] = Field(default_factory=list)


class ValidationFinding(BaseModel):
    """Finding produced by static validation tools (cfn-lint/checkov/etc.)."""

    model_config = ConfigDict(extra="forbid")

    check_id: str
    message: str
    severity: FindingSeverity
    resource_logical_id: str | None = None
    stage: str | None = None


class RiskScore(BaseModel):
    """Deterministic risk score assigned by planning stage."""

    model_config = ConfigDict(extra="forbid")

    resource_id: str
    score: int = Field(ge=0, le=100)
    rationale: list[str] = Field(default_factory=list)


class MigrationPlanItem(BaseModel):
    """Plan action and review requirement for one source resource."""

    model_config = ConfigDict(extra="forbid")

    resource_id: str
    action: str
    status: PlanItemStatus
    risk_score: int = Field(ge=0, le=100)
    notes: list[str] = Field(default_factory=list)


class MigrationPlanReport(BaseModel):
    """Rendered migration planning summary emitted by planner stage."""

    model_config = ConfigDict(extra="forbid")

    plan_hash: str
    total_items: int = Field(ge=0)
    auto_migratable_count: int = Field(ge=0)
    requires_review_count: int = Field(ge=0)
    high_risk_count: int = Field(ge=0)
    markdown_summary: str
    html_summary: str


class ApprovalRecord(BaseModel):
    """Recorded human decision tied to a plan hash."""

    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision
    reviewer: str
    timestamp: datetime
    plan_hash: str
    comments: str | None = None


class DeploymentResult(BaseModel):
    """Outcome of deployment execution."""

    model_config = ConfigDict(extra="forbid")

    deployed: bool
    stack_name: str | None = None
    stack_id: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    message: str | None = None
    timestamp: datetime | None = None


class PostDeployFinding(BaseModel):
    """Result of post-deployment equivalence/smoke checks."""

    model_config = ConfigDict(extra="forbid")

    check_id: str
    passed: bool
    message: str
    severity: FindingSeverity = FindingSeverity.MEDIUM


class AuditRecord(BaseModel):
    """Audit event emitted by each stage transition."""

    model_config = ConfigDict(extra="forbid")

    inputs_hash: str
    outputs: dict[str, Any]
    rule_ids_used: list[str] = Field(default_factory=list)
    reviewer: str | None = None
    timestamp: datetime


class MigrationState(BaseModel):
    """Top-level shared state flowing through the LangGraph pipeline."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    started_at: datetime
    updated_at: datetime
    discovered_resources: list[DiscoveredResource] = Field(default_factory=list)
    parsed_resources: list[ParsedResource] = Field(default_factory=list)
    parsed_dependency_graph: ResourceDependencyGraph | None = None
    mapping_results: list[MappingResult] = Field(default_factory=list)
    cfn_artifacts: list[CfnArtifact] = Field(default_factory=list)
    validation_findings: list[ValidationFinding] = Field(default_factory=list)
    risk_scores: list[RiskScore] = Field(default_factory=list)
    migration_plan: list[MigrationPlanItem] = Field(default_factory=list)
    plan_hash: str | None = None
    migration_plan_report: MigrationPlanReport | None = None
    approval_record: ApprovalRecord | None = None
    deployment_result: DeploymentResult | None = None
    post_deploy_findings: list[PostDeployFinding] = Field(default_factory=list)
    audit_records: list[AuditRecord] = Field(default_factory=list)
    manual_dependencies: list[str] = Field(default_factory=list)
