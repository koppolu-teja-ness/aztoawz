from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import zipfile

import boto3
from moto import mock_aws

from src.agents.postvalidate import PostValidateRequest, postvalidate_node
from src.graph.state import CfnArtifact, DeploymentResult, MigrationState, ParsedResource


def _lambda_zip_bytes() -> bytes:
    source = """
def handler(event, context):
    return {"ok": True, "echo": event.get("smoke", False)}
""".strip()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr("index.py", source)
    return buffer.getvalue()


def _build_template() -> str:
    return """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  MigratedSecret:
    Type: AWS::SecretsManager::Secret
    Properties:
      Name: migrated-secret
  MigratedFunction:
    Type: AWS::Lambda::Function
    Properties:
      FunctionName: migrated-fn
      Runtime: python3.11
      Handler: index.handler
  MigrationSecurityGroup:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupName: migration-sg
      GroupDescription: migration security group
      SecurityGroupIngress:
        - IpProtocol: tcp
          FromPort: 443
          ToPort: 443
          CidrIp: 10.0.0.0/24
      SecurityGroupEgress:
        - IpProtocol: tcp
          FromPort: 443
          ToPort: 443
          CidrIp: 10.0.1.0/24
""".strip()


def _base_state() -> MigrationState:
    now = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)
    return MigrationState(
        run_id="run-postvalidate-001",
        started_at=now,
        updated_at=now,
        parsed_resources=[
            ParsedResource(
                resource_id="src-rbac-1",
                source_type="Microsoft.Authorization/roleAssignments",
                normalized_type="roleAssignment",
                properties={
                    "allowed_iam_actions": [
                        "logs:CreateLogGroup",
                        "logs:CreateLogStream",
                        "logs:PutLogEvents",
                    ],
                    "allowed_iam_resources": ["*"],
                    "allow_wildcard_actions": False,
                    "allow_wildcard_resources": True,
                    "allow_public_ingress": False,
                    "allow_public_egress": False,
                },
                dependencies=[],
            )
        ],
        cfn_artifacts=[
            CfnArtifact(
                logical_id="PostValidateStack",
                resource_type="AWS::CloudFormation::Stack",
                properties={"template_yaml": _build_template()},
                rule_ids_used=["fn-001", "kv-002", "vnet-003"],
            )
        ],
        deployment_result=DeploymentResult(
            deployed=True,
            stack_name="postvalidate-stack",
            stack_id="stack-123",
            outputs={},
            stack_statuses={"postvalidate-stack": "CREATE_COMPLETE"},
            resource_statuses={"postvalidate-stack": {}},
            rollback_triggered=False,
            rollback_actions=[],
            failed_stack=None,
            message="deployed",
            timestamp=now,
        ),
    )


def _setup_clean_environment(region: str) -> None:
    secrets_client = boto3.client("secretsmanager", region_name=region)
    iam_client = boto3.client("iam", region_name=region)
    lambda_client = boto3.client("lambda", region_name=region)
    ec2_client = boto3.client("ec2", region_name=region)

    secrets_client.create_secret(Name="migrated-secret", SecretString="placeholder")

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    role = iam_client.create_role(
        RoleName="fn-exec-role",
        AssumeRolePolicyDocument=json.dumps(trust_policy),
    )
    iam_client.put_role_policy(
        RoleName="fn-exec-role",
        PolicyName="least-priv-policy",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "logs:CreateLogGroup",
                            "logs:CreateLogStream",
                            "logs:PutLogEvents",
                        ],
                        "Resource": "*",
                    }
                ],
            }
        ),
    )

    lambda_client.create_function(
        FunctionName="migrated-fn",
        Runtime="python3.11",
        Role=role["Role"]["Arn"],
        Handler="index.handler",
        Code={"ZipFile": _lambda_zip_bytes()},
        Timeout=3,
        MemorySize=128,
        Publish=True,
    )

    vpc_id = ec2_client.describe_vpcs()["Vpcs"][0]["VpcId"]
    group = ec2_client.create_security_group(
        GroupName="migration-sg",
        Description="migration security group",
        VpcId=vpc_id,
    )
    group_id = group["GroupId"]

    ec2_client.revoke_security_group_egress(
        GroupId=group_id,
        IpPermissions=[
            {
                "IpProtocol": "-1",
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )

    ec2_client.authorize_security_group_ingress(
        GroupId=group_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "IpRanges": [{"CidrIp": "10.0.0.0/24"}],
            }
        ],
    )

    ec2_client.authorize_security_group_egress(
        GroupId=group_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "IpRanges": [{"CidrIp": "10.0.1.0/24"}],
            }
        ],
    )


def _introduce_over_permissive_regression(region: str) -> None:
    iam_client = boto3.client("iam", region_name=region)
    ec2_client = boto3.client("ec2", region_name=region)

    iam_client.put_role_policy(
        RoleName="fn-exec-role",
        PolicyName="least-priv-policy",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "*",
                        "Resource": "*",
                    }
                ],
            }
        ),
    )

    group = ec2_client.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": ["migration-sg"]}]
    )["SecurityGroups"][0]

    ec2_client.authorize_security_group_ingress(
        GroupId=group["GroupId"],
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )


def test_postvalidate_clean_fixture_passes_structural_functional_and_security_checks() -> None:
    region = "us-east-1"

    with mock_aws():
        _setup_clean_environment(region)

        updated = postvalidate_node(
            _base_state(),
            PostValidateRequest(region=region, lambda_test_event={"smoke": True}),
            now_provider=lambda: datetime(2026, 9, 29, 16, 1, tzinfo=timezone.utc),
        )

        assert updated.post_deploy_findings
        assert all(item.passed for item in updated.post_deploy_findings)

        check_ids = {item.check_id for item in updated.post_deploy_findings}
        assert "SECURITY_POSTURE_SUMMARY" in check_ids
        assert any(item.check_id.startswith("SMOKE_LAMBDA_INVOKE") for item in updated.post_deploy_findings)
        assert any(item.check_id.startswith("STRUCT_COUNT_AWS::Lambda::Function") for item in updated.post_deploy_findings)

        audit = updated.audit_records[-1]
        assert audit.outputs["stage"] == "postvalidate"
        assert audit.outputs["validation_passed"] is True
        assert audit.outputs["security_failure_count"] == 0


def test_postvalidate_over_permissive_fixture_catches_security_regression() -> None:
    region = "us-east-1"

    with mock_aws():
        _setup_clean_environment(region)
        _introduce_over_permissive_regression(region)

        updated = postvalidate_node(
            _base_state(),
            PostValidateRequest(region=region, lambda_test_event={"smoke": True}),
            now_provider=lambda: datetime(2026, 9, 29, 16, 2, tzinfo=timezone.utc),
        )

        failed = [item for item in updated.post_deploy_findings if not item.passed]
        assert failed

        assert any(item.check_id.startswith("SECURITY_PUBLIC_INGRESS") for item in failed)
        assert any(item.check_id.startswith("SECURITY_IAM_ACTION_WILDCARD") for item in failed)
        assert any(item.check_id == "SECURITY_POSTURE_SUMMARY" for item in failed)

        audit = updated.audit_records[-1]
        assert audit.outputs["validation_passed"] is False
        assert audit.outputs["security_failure_count"] >= 1


def test_postvalidate_skips_gracefully_when_deployment_failed() -> None:
    state = _base_state().model_copy(
        update={
            "deployment_result": DeploymentResult(
                deployed=False,
                stack_name="postvalidate-stack",
                rollback_triggered=True,
                rollback_actions=["delete:postvalidate-stack:CREATE_FAILED"],
                failed_stack="postvalidate-stack",
                message="Deployment halted after failure in stack postvalidate-stack.",
                timestamp=datetime(2026, 9, 29, 16, 3, tzinfo=timezone.utc),
            )
        }
    )

    updated = postvalidate_node(
        state,
        PostValidateRequest(region="us-east-1"),
        now_provider=lambda: datetime(2026, 9, 29, 16, 4, tzinfo=timezone.utc),
    )

    assert updated.post_deploy_findings
    assert updated.post_deploy_findings[0].check_id == "POSTVALIDATE_SKIPPED_DEPLOYMENT_NOT_SUCCESSFUL"
    assert updated.post_deploy_findings[0].passed is False

    audit = updated.audit_records[-1]
    assert audit.outputs["stage"] == "postvalidate"
    assert audit.outputs["validation_passed"] is False
