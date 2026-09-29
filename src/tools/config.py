"""Typed configuration loader for migration settings."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.graph.state import ApprovalDecision


class NamingConventions(BaseModel):
    """Resource naming policy used by generators."""

    model_config = ConfigDict(extra="forbid")

    prefix: str
    workload: str
    separator: str = "-"
    max_length: int = Field(default=63, ge=1, le=128)


class RiskThresholds(BaseModel):
    """Cutoffs used to derive AUTO/REVIEW/HIGH_RISK status."""

    model_config = ConfigDict(extra="forbid")

    auto_migratable_max_score: int = Field(ge=0, le=100)
    requires_review_max_score: int = Field(ge=0, le=100)
    high_risk_min_score: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def validate_ordering(self) -> "RiskThresholds":
        if self.auto_migratable_max_score > self.requires_review_max_score:
            raise ValueError(
                "auto_migratable_max_score must be <= requires_review_max_score"
            )
        if self.requires_review_max_score >= self.high_risk_min_score:
            raise ValueError("requires_review_max_score must be < high_risk_min_score")
        return self


class ApprovalGateConfig(BaseModel):
    """Human-gate controls and permitted decision values."""

    model_config = ConfigDict(extra="forbid")

    require_signed_approval_artifact: bool = True
    approval_artifact_ttl_hours: int = Field(default=72, ge=1)
    allowed_decisions: list[ApprovalDecision] = Field(
        default_factory=lambda: [
            ApprovalDecision.APPROVE,
            ApprovalDecision.REJECT,
            ApprovalDecision.MODIFY,
        ]
    )


class MappingConfig(BaseModel):
    """Mapping retrieval controls for rule-confidence gating."""

    model_config = ConfigDict(extra="forbid")

    min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    candidates_per_resource: int = Field(default=3, ge=1, le=20)


class ResourcePolicyConfig(BaseModel):
    """Policy defaults and per-resource overrides for generated CFN resources."""

    model_config = ConfigDict(extra="forbid")

    default: str = "Delete"
    overrides: dict[str, str] = Field(default_factory=dict)


class CloudFormationConfig(BaseModel):
    """CloudFormation-specific generation settings."""

    model_config = ConfigDict(extra="forbid")

    deletion_policy: ResourcePolicyConfig = Field(default_factory=ResourcePolicyConfig)
    update_replace_policy: ResourcePolicyConfig = Field(default_factory=ResourcePolicyConfig)


class MigrationConfig(BaseModel):
    """Root configuration model for migration runtime policies."""

    model_config = ConfigDict(extra="forbid")

    naming_conventions: NamingConventions
    tagging_standard: dict[str, str]
    azure_to_aws_region_map: dict[str, str]
    risk_thresholds: RiskThresholds
    project: dict[str, str] = Field(default_factory=dict)
    security: dict[str, bool] = Field(default_factory=dict)
    mapping: MappingConfig = Field(default_factory=MappingConfig)
    cloudformation: CloudFormationConfig = Field(default_factory=CloudFormationConfig)
    approval_gate: ApprovalGateConfig = Field(default_factory=ApprovalGateConfig)
    validation: dict[str, bool] = Field(default_factory=dict)
    audit: dict[str, bool] = Field(default_factory=dict)


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "migration.config.yaml"


def load_migration_config(path: str | Path | None = None) -> MigrationConfig:
    """Load and validate migration configuration from YAML."""

    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(f"Migration config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as config_file:
        payload = yaml.safe_load(config_file) or {}

    if not isinstance(payload, dict):
        raise ValueError("Migration config must deserialize to a mapping")

    return MigrationConfig.model_validate(payload)
