"""Pydantic API schemas for migration lifecycle endpoints."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.graph.state import ApprovalDecision, ApprovalRecord, PlanItemStatus


class MigrationRunStatus(str, Enum):
    """Lifecycle status for API-tracked migration runs."""

    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    MODIFY_REQUESTED = "MODIFY_REQUESTED"


class MigrationStartRequest(BaseModel):
    """Input payload for starting a migration run against a real Bicep source."""

    model_config = ConfigDict(extra="forbid")

    source_reference: str = Field(default="bicep-source", min_length=1)
    bicep_path: str = Field(min_length=1)
    use_live_azure: bool = False
    subscription_id: str | None = None
    resource_group: str | None = None


class MigrationStartResponse(BaseModel):
    """Response payload returned after creating a migration run."""

    model_config = ConfigDict(extra="forbid")

    migration_id: str
    status: MigrationRunStatus
    plan_hash: str
    created_at: datetime


class MigrationStatusCounts(BaseModel):
    """Summary counters used by status and plan responses."""

    model_config = ConfigDict(extra="forbid")

    total_items: int
    auto_migratable_count: int
    requires_review_count: int
    high_risk_count: int


class MigrationStatusResponse(BaseModel):
    """Current migration status plus aggregate planning counters."""

    model_config = ConfigDict(extra="forbid")

    migration_id: str
    status: MigrationRunStatus
    plan_hash: str
    created_at: datetime
    updated_at: datetime
    summary: MigrationStatusCounts
    has_approval_record: bool


class MigrationPlanItemResponse(BaseModel):
    """One migration plan row in API responses."""

    model_config = ConfigDict(extra="forbid")

    resource_id: str
    action: str
    status: PlanItemStatus
    risk_score: int
    rule_id: str | None = None
    notes: list[str] = Field(default_factory=list)


class MigrationPlanResponse(BaseModel):
    """Detailed migration plan and summarized report payload."""

    model_config = ConfigDict(extra="forbid")

    migration_id: str
    plan_hash: str
    summary: MigrationStatusCounts
    items: list[MigrationPlanItemResponse] = Field(default_factory=list)
    markdown_summary: str
    html_summary: str


class MigrationApprovalRequest(BaseModel):
    """Approval decision payload bound to a specific plan hash."""

    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision
    plan_hash: str = Field(min_length=1)
    rationale: str | None = None

    @model_validator(mode="after")
    def validate_rationale_for_non_approve(self) -> "MigrationApprovalRequest":
        if self.decision in {ApprovalDecision.REJECT, ApprovalDecision.MODIFY}:
            if not self.rationale or not self.rationale.strip():
                raise ValueError("rationale is required for REJECT or MODIFY decisions")
        return self


class MigrationApprovalResponse(BaseModel):
    """Stored approval record and updated run status."""

    model_config = ConfigDict(extra="forbid")

    migration_id: str
    status: MigrationRunStatus
    approval_record: ApprovalRecord


class MigrationReportsResponse(BaseModel):
    """Report discovery payload for generated migration artifacts."""

    model_config = ConfigDict(extra="forbid")

    migration_id: str
    plan_hash: str
    report_paths: list[str] = Field(default_factory=list)
    has_approval_record: bool
    generated_at: datetime
