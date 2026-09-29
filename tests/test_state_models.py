from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.graph.state import (
    ApprovalDecision,
    ApprovalRecord,
    AuditRecord,
    CfnArtifact,
    DiscoveredResource,
    DeploymentResult,
    FindingSeverity,
    MappingResult,
    MigrationPlanItem,
    MigrationState,
    ParsedResource,
    PlanItemStatus,
    PostDeployFinding,
    RiskScore,
    ValidationFinding,
)


def test_approval_record_requires_plan_hash() -> None:
    with pytest.raises(ValidationError):
        ApprovalRecord.model_validate(
            {
                "decision": ApprovalDecision.APPROVE,
                "reviewer": "reviewer@example.com",
                "timestamp": datetime.now(tz=timezone.utc),
            }
        )


def test_plan_item_status_rejects_invalid_value() -> None:
    with pytest.raises(ValidationError):
        MigrationPlanItem.model_validate(
            {
                "resource_id": "res-1",
                "action": "Generate CloudFormation",
                "status": "MANUAL",
                "risk_score": 10,
            }
        )


def test_mapping_result_confidence_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError):
        MappingResult(
            source_resource_id="/subscriptions/123/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/v1",
            target_resource_type="AWS::SecretsManager::Secret",
            mapped=True,
            rule_id="KV-001",
            confidence=1.1,
            caveats=[],
        )


def test_migration_state_serialization_round_trip() -> None:
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    state = MigrationState(
        run_id="run-001",
        started_at=now,
        updated_at=now,
        discovered_resources=[
            DiscoveredResource(
                resource_id="azure-keyvault-1",
                resource_type="Microsoft.KeyVault/vaults",
                name="kv-prod",
                resource_group="rg-platform",
                region="eastus",
                tags={"owner": "platform-team"},
                raw_properties={"tenantId": "00000000-0000-0000-0000-000000000000"},
            )
        ],
        parsed_resources=[
            ParsedResource(
                resource_id="azure-keyvault-1",
                source_type="Microsoft.KeyVault/vaults",
                normalized_type="key_vault",
                properties={"softDelete": True},
                dependencies=[],
            )
        ],
        mapping_results=[
            MappingResult(
                source_resource_id="azure-keyvault-1",
                target_resource_type="AWS::SecretsManager::Secret",
                mapped=True,
                rule_id="KV-001",
                confidence=0.97,
                caveats=["Rotate policy must be validated."],
            )
        ],
        cfn_artifacts=[
            CfnArtifact(
                logical_id="KeyVaultSecret1",
                resource_type="AWS::SecretsManager::Secret",
                properties={"Name": "kv-prod-secret"},
                rule_ids_used=["KV-001"],
            )
        ],
        validation_findings=[
            ValidationFinding(
                check_id="CFN_LINT_E3001",
                message="Template is valid",
                severity=FindingSeverity.LOW,
                resource_logical_id="KeyVaultSecret1",
                stage="static-validation",
            )
        ],
        risk_scores=[
            RiskScore(
                resource_id="azure-keyvault-1",
                score=22,
                rationale=["Scoped IAM with no wildcard permissions."],
            )
        ],
        migration_plan=[
            MigrationPlanItem(
                resource_id="azure-keyvault-1",
                action="Deploy mapped secret",
                status=PlanItemStatus.AUTO,
                risk_score=22,
                notes=[],
            )
        ],
        approval_record=ApprovalRecord(
            decision=ApprovalDecision.APPROVE,
            reviewer="reviewer@example.com",
            timestamp=now,
            plan_hash="sha256:abc123",
            comments="Looks good.",
        ),
        deployment_result=DeploymentResult(
            deployed=True,
            stack_name="mig-stack-dev",
            stack_id="arn:aws:cloudformation:us-east-1:123456789012:stack/mig-stack-dev/abcd",
            outputs={"SecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:kv-prod"},
            message="Deployed successfully",
            timestamp=now,
        ),
        post_deploy_findings=[
            PostDeployFinding(
                check_id="equivalence-secret-metadata",
                passed=True,
                message="Secret metadata equivalent",
                severity=FindingSeverity.LOW,
            )
        ],
        audit_records=[
            AuditRecord(
                inputs_hash="sha256:def456",
                outputs={"mapped_count": 1},
                rule_ids_used=["KV-001"],
                reviewer="reviewer@example.com",
                timestamp=now,
            )
        ],
        manual_dependencies=["Unsupported Azure service Microsoft.Sql/servers"],
    )

    serialized = state.model_dump(mode="json")
    restored = MigrationState.model_validate(serialized)

    assert restored == state
