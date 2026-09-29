from __future__ import annotations

from datetime import datetime, timezone

import boto3
from moto import mock_aws

from src.agents.deployer import DeploymentRequest, deployer_node
from src.graph.state import (
    ApprovalDecision,
    ApprovalRecord,
    CfnArtifact,
    MigrationState,
)


def _base_state(artifacts: list[CfnArtifact], plan_hash: str = "sha256:plan-deploy") -> MigrationState:
    now = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
    return MigrationState(
        run_id="run-deploy-001",
        started_at=now,
        updated_at=now,
        plan_hash=plan_hash,
        approval_record=ApprovalRecord(
            decision=ApprovalDecision.APPROVE,
            reviewer="reviewer-1",
            timestamp=now,
            plan_hash=plan_hash,
            comments="approved for deployment",
        ),
        cfn_artifacts=artifacts,
    )


def _stack_exists(client: object, stack_name: str) -> bool:
    assert hasattr(client, "describe_stacks")
    try:
        client.describe_stacks(StackName=stack_name)
        return True
    except Exception:
        return False


def test_deployer_node_deploys_multiple_stacks_in_dependency_order() -> None:
    foundation_template = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  FoundationBucket:
    Type: AWS::S3::Bucket
""".strip()

    app_template = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  AppTable:
    Type: AWS::DynamoDB::Table
    Properties:
      BillingMode: PAY_PER_REQUEST
      AttributeDefinitions:
        - AttributeName: id
          AttributeType: S
      KeySchema:
        - AttributeName: id
          KeyType: HASH
""".strip()

    artifacts = [
        CfnArtifact(
            logical_id="AppStack",
            resource_type="AWS::CloudFormation::Stack",
            properties={
                "stack_name": "app-stack",
                "template_yaml": app_template,
                "depends_on_stacks": ["foundation-stack"],
                "unit": "functions",
            },
            rule_ids_used=["FN-001"],
        ),
        CfnArtifact(
            logical_id="FoundationStack",
            resource_type="AWS::CloudFormation::Stack",
            properties={
                "stack_name": "foundation-stack",
                "template_yaml": foundation_template,
                "unit": "networking",
            },
            rule_ids_used=["VNET-001"],
        ),
    ]

    with mock_aws():
        client = boto3.client("cloudformation", region_name="us-east-1")
        updated = deployer_node(
            _base_state(artifacts),
            DeploymentRequest(region="us-east-1", poll_interval_seconds=0.0, max_poll_attempts=20),
            cloudformation_client=client,
            now_provider=lambda: datetime(2026, 9, 29, 15, 1, tzinfo=timezone.utc),
            sleep_provider=lambda _: None,
        )

        assert updated.deployment_result is not None
        result = updated.deployment_result

        assert result.deployed is True
        assert result.rollback_triggered is False
        assert list(result.stack_statuses.keys()) == ["foundation-stack", "app-stack"]
        assert result.stack_statuses["foundation-stack"] == "CREATE_COMPLETE"
        assert result.stack_statuses["app-stack"] == "CREATE_COMPLETE"
        assert result.resource_statuses["foundation-stack"]
        assert result.resource_statuses["app-stack"]
        assert any(
            status.endswith("COMPLETE")
            for status in result.resource_statuses["foundation-stack"].values()
        )
        assert any(
            status.endswith("COMPLETE")
            for status in result.resource_statuses["app-stack"].values()
        )

        audit = updated.audit_records[-1]
        assert audit.outputs["stage"] == "deployer"
        assert audit.outputs["deployed_stacks"] == ["foundation-stack", "app-stack"]
        assert audit.outputs["approval_reference"]["plan_hash"] == "sha256:plan-deploy"


def test_deployer_node_rolls_back_after_mid_sequence_failure_and_halts_downstream() -> None:
    good_template = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  GoodBucket:
    Type: AWS::S3::Bucket
""".strip()

    malformed_template = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
    BrokenResource: [
""".strip()

    skipped_template = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  NeverCreatedBucket:
    Type: AWS::S3::Bucket
""".strip()

    artifacts = [
        CfnArtifact(
            logical_id="FoundationStack",
            resource_type="AWS::CloudFormation::Stack",
            properties={
                "stack_name": "foundation-stack",
                "template_yaml": good_template,
                "unit": "networking",
            },
            rule_ids_used=["VNET-001"],
        ),
        CfnArtifact(
            logical_id="BrokenStack",
            resource_type="AWS::CloudFormation::Stack",
            properties={
                "stack_name": "broken-stack",
                "template_yaml": malformed_template,
                "depends_on_stacks": ["foundation-stack"],
                "unit": "functions",
            },
            rule_ids_used=["FN-001"],
        ),
        CfnArtifact(
            logical_id="SkippedStack",
            resource_type="AWS::CloudFormation::Stack",
            properties={
                "stack_name": "skipped-stack",
                "template_yaml": skipped_template,
                "depends_on_stacks": ["broken-stack"],
                "unit": "keyvault",
            },
            rule_ids_used=["KV-001"],
        ),
    ]

    with mock_aws():
        client = boto3.client("cloudformation", region_name="us-east-1")
        updated = deployer_node(
            _base_state(artifacts),
            DeploymentRequest(region="us-east-1", poll_interval_seconds=0.0, max_poll_attempts=20),
            cloudformation_client=client,
            now_provider=lambda: datetime(2026, 9, 29, 15, 2, tzinfo=timezone.utc),
            sleep_provider=lambda _: None,
        )

        assert updated.deployment_result is not None
        result = updated.deployment_result

        assert result.deployed is False
        assert result.rollback_triggered is True
        assert result.failed_stack == "broken-stack"
        assert "skipped-stack" not in result.stack_statuses
        assert any(action.startswith("delete:foundation-stack") for action in result.rollback_actions)
        assert _stack_exists(client, "foundation-stack") is False
        assert _stack_exists(client, "skipped-stack") is False

        audit = updated.audit_records[-1]
        assert audit.outputs["rollback_triggered"] is True
        assert audit.outputs["attempted_stacks"] == ["foundation-stack", "broken-stack"]


def _single_artifact() -> list[CfnArtifact]:
    return [
        CfnArtifact(
            logical_id="AppStack",
            resource_type="AWS::CloudFormation::Stack",
            properties={
                "stack_name": "app-stack",
                "template_yaml": (
                    "AWSTemplateFormatVersion: '2010-09-09'\n"
                    "Resources:\n"
                    "  AppBucket:\n"
                    "    Type: AWS::S3::Bucket\n"
                ),
                "unit": "functions",
            },
            rule_ids_used=["FN-001"],
        )
    ]


def test_deployer_node_rejects_missing_approval() -> None:
    state = _base_state(_single_artifact()).model_copy(update={"approval_record": None})

    with mock_aws():
        client = boto3.client("cloudformation", region_name="us-east-1")
        try:
            deployer_node(
                state,
                DeploymentRequest(region="us-east-1", poll_interval_seconds=0.0),
                cloudformation_client=client,
                sleep_provider=lambda _: None,
            )
            raise AssertionError("Expected RuntimeError for missing approval artifact")
        except RuntimeError as exc:
            assert "missing approval artifact" in str(exc)

        assert _stack_exists(client, "app-stack") is False


def test_deployer_node_rejects_mismatched_plan_hash() -> None:
    state = _base_state(_single_artifact(), plan_hash="sha256:plan-current").model_copy(
        update={
            "approval_record": ApprovalRecord(
                decision=ApprovalDecision.APPROVE,
                reviewer="reviewer-1",
                timestamp=datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc),
                plan_hash="sha256:plan-stale",
                comments="approved a different, older plan",
            )
        }
    )

    with mock_aws():
        client = boto3.client("cloudformation", region_name="us-east-1")
        try:
            deployer_node(
                state,
                DeploymentRequest(region="us-east-1", poll_interval_seconds=0.0),
                cloudformation_client=client,
                sleep_provider=lambda _: None,
            )
            raise AssertionError("Expected RuntimeError for mismatched plan_hash")
        except RuntimeError as exc:
            assert "plan_hash mismatch" in str(exc)

        assert _stack_exists(client, "app-stack") is False


def test_deployer_node_rejects_non_approve_decision() -> None:
    plan_hash = "sha256:plan-deploy"
    state = _base_state(_single_artifact(), plan_hash=plan_hash).model_copy(
        update={
            "approval_record": ApprovalRecord(
                decision=ApprovalDecision.REJECT,
                reviewer="reviewer-1",
                timestamp=datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc),
                plan_hash=plan_hash,
                comments="rejected by reviewer",
            )
        }
    )

    with mock_aws():
        client = boto3.client("cloudformation", region_name="us-east-1")
        try:
            deployer_node(
                state,
                DeploymentRequest(region="us-east-1", poll_interval_seconds=0.0),
                cloudformation_client=client,
                sleep_provider=lambda _: None,
            )
            raise AssertionError("Expected RuntimeError for non-APPROVE decision")
        except RuntimeError as exc:
            assert "decision must be APPROVE" in str(exc)

        assert _stack_exists(client, "app-stack") is False
