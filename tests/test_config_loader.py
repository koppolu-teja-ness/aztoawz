from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from src.graph.state import ApprovalDecision
from src.tools.config import load_migration_config


def test_load_migration_config_from_path(tmp_path: Path) -> None:
    config_file = tmp_path / "migration.config.yaml"
    config_file.write_text(
        """
project:
  name: test-project
  environment: dev
naming_conventions:
  prefix: mig
  workload: app
  separator: "-"
  max_length: 63
tagging_standard:
  owner: platform-team
  data_classification: internal
azure_to_aws_region_map:
  eastus: us-east-1
risk_thresholds:
  auto_migratable_max_score: 20
  requires_review_max_score: 60
  high_risk_min_score: 61
approval_gate:
  require_signed_approval_artifact: true
  approval_artifact_ttl_hours: 72
  allowed_decisions:
    - APPROVE
    - REJECT
    - MODIFY
""".strip(),
        encoding="utf-8",
    )

    config = load_migration_config(config_file)

    assert config.naming_conventions.prefix == "mig"
    assert config.azure_to_aws_region_map["eastus"] == "us-east-1"
    assert config.approval_gate.allowed_decisions == [
        ApprovalDecision.APPROVE,
        ApprovalDecision.REJECT,
        ApprovalDecision.MODIFY,
    ]


def test_load_migration_config_rejects_invalid_threshold_order(tmp_path: Path) -> None:
    config_file = tmp_path / "migration.config.yaml"
    config_file.write_text(
        """
naming_conventions:
  prefix: mig
  workload: app
  separator: "-"
  max_length: 63
tagging_standard:
  owner: platform-team
azure_to_aws_region_map:
  eastus: us-east-1
risk_thresholds:
  auto_migratable_max_score: 70
  requires_review_max_score: 60
  high_risk_min_score: 61
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_migration_config(config_file)
