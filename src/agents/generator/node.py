"""CloudFormation generator node for stack-level template emission."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.graph.state import (
    AuditRecord,
    CfnArtifact,
    MappingResult,
    MigrationState,
    ParsedResource,
    ValidationFinding,
)
from src.tools.config import load_migration_config


class GeneratorRequest(BaseModel):
    """Input contract for CloudFormation generation stage."""

    model_config = ConfigDict(extra="forbid")

    config_path: str | None = None
    validation_feedback: list[ValidationFinding] = Field(default_factory=list)
    retry_attempt: int = Field(default=0, ge=0, le=5)


NowCallable = Callable[[], datetime]

_TEMPLATE_VERSION = "2010-09-09"
_UNIT_KEYVAULT = "keyvault"
_UNIT_FUNCTIONS = "functions"
_UNIT_NETWORKING = "networking"


def generator_node(
    state: MigrationState,
    request: GeneratorRequest,
    *,
    now_provider: NowCallable | None = None,
) -> MigrationState:
    """Generate one CloudFormation template artifact per logical migration unit."""

    now_fn = now_provider or (lambda: datetime.now(tz=timezone.utc))
    config = load_migration_config(request.config_path)
    parsed_by_id = {resource.resource_id: resource for resource in state.parsed_resources}
    harden_least_privilege = _feedback_mentions(
        request.validation_feedback,
        (
            "least-privilege",
            "least privilege",
            "wildcard",
            "action '*'",
            "resource '*'",
        ),
    )
    harden_public_access = _feedback_mentions(
        request.validation_feedback,
        (
            "public",
            "internet",
            "0.0.0.0/0",
            "world",
            "all users",
            "anonymous",
        ),
    )

    grouped_results: dict[str, list[MappingResult]] = defaultdict(list)
    for result in state.mapping_results:
        if not result.mapped:
            continue
        unit = _resolve_unit(result, parsed_by_id)
        if unit is None:
            continue
        grouped_results[unit].append(result)

    artifacts: list[CfnArtifact] = []
    collected_rule_ids: list[str] = []

    networking_exports = _networking_export_names(config.naming_conventions.prefix)
    networking_required = _UNIT_NETWORKING in grouped_results

    for unit in (_UNIT_KEYVAULT, _UNIT_FUNCTIONS, _UNIT_NETWORKING):
        unit_results = grouped_results.get(unit, [])
        if not unit_results:
            continue

        template = _build_base_template(config.tagging_standard)
        resources = template["Resources"]
        outputs = template["Outputs"]

        if unit == _UNIT_KEYVAULT:
            _append_keyvault_resources(
                resources,
                outputs,
                unit_results,
                config.tagging_standard,
                config.naming_conventions.prefix,
                config.cloudformation.deletion_policy.default,
                config.cloudformation.deletion_policy.overrides,
                config.cloudformation.update_replace_policy.default,
                config.cloudformation.update_replace_policy.overrides,
                harden_least_privilege,
            )
        elif unit == _UNIT_FUNCTIONS:
            _append_function_resources(
                resources,
                outputs,
                unit_results,
                config.tagging_standard,
                config.naming_conventions.prefix,
                config.cloudformation.deletion_policy.default,
                config.cloudformation.deletion_policy.overrides,
                config.cloudformation.update_replace_policy.default,
                config.cloudformation.update_replace_policy.overrides,
                networking_required,
                networking_exports,
                harden_public_access,
            )
        elif unit == _UNIT_NETWORKING:
            _append_networking_resources(
                resources,
                outputs,
                unit_results,
                config.tagging_standard,
                config.naming_conventions.prefix,
                config.cloudformation.deletion_policy.default,
                config.cloudformation.deletion_policy.overrides,
                config.cloudformation.update_replace_policy.default,
                config.cloudformation.update_replace_policy.overrides,
                networking_exports,
            )

        template_yaml = yaml.safe_dump(template, sort_keys=False)
        stack_logical_id = _stack_logical_id(unit)
        stack_name = f"{config.naming_conventions.prefix}-{unit}-stack"
        unit_rule_ids = sorted({result.rule_id for result in unit_results})
        collected_rule_ids.extend(unit_rule_ids)

        artifacts.append(
            CfnArtifact(
                logical_id=stack_logical_id,
                resource_type="AWS::CloudFormation::Stack",
                properties={
                    "stack_name": stack_name,
                    "unit": unit,
                    "template_yaml": template_yaml,
                },
                rule_ids_used=unit_rule_ids,
            )
        )

    timestamp = now_fn()
    audit = AuditRecord(
        inputs_hash=_stable_hash(
            {
                "run_id": state.run_id,
                "request": request.model_dump(mode="json"),
                "mapping_results": [
                    mapping.model_dump(mode="json") for mapping in state.mapping_results
                ],
            }
        ),
        outputs={
            "stage": "generator",
            "stack_count": len(artifacts),
            "artifact_logical_ids": [artifact.logical_id for artifact in artifacts],
            "retry_attempt": request.retry_attempt,
            "feedback_count": len(request.validation_feedback),
        },
        rule_ids_used=sorted(set(collected_rule_ids)),
        reviewer=None,
        timestamp=timestamp,
    )

    return state.model_copy(
        update={
            "updated_at": timestamp,
            "cfn_artifacts": artifacts,
            "audit_records": [*state.audit_records, audit],
        }
    )


def _build_base_template(tagging_standard: dict[str, str]) -> dict[str, Any]:
    return {
        "AWSTemplateFormatVersion": _TEMPLATE_VERSION,
        "Description": "Generated by Azure-to-AWS migration assistant.",
        "Parameters": {
            "ProjectName": {"Type": "String", "Default": tagging_standard.get("migration_project", "migration")},
            "EnvironmentName": {"Type": "String", "Default": "dev"},
        },
        "Mappings": {
            "TagDefaults": {
                "Values": {
                    "Owner": tagging_standard.get("owner", "platform-team"),
                    "CostCenter": tagging_standard.get("cost_center", "engineering"),
                    "DataClassification": tagging_standard.get("data_classification", "internal"),
                    "ManagedBy": tagging_standard.get("managed_by", "migration-assistant"),
                    "MigrationProject": tagging_standard.get("migration_project", "azure-to-aws"),
                }
            }
        },
        "Conditions": {
            "HasProjectName": {
                "Fn::Not": [
                    {
                        "Fn::Equals": [
                            {"Ref": "ProjectName"},
                            "",
                        ]
                    }
                ]
            }
        },
        "Resources": {},
        "Outputs": {},
    }


def _stack_logical_id(unit: str) -> str:
    if unit == _UNIT_KEYVAULT:
        return "KeyVaultStack"
    if unit == _UNIT_FUNCTIONS:
        return "FunctionsStack"
    return "NetworkingStack"


def _resolve_unit(
    result: MappingResult,
    parsed_by_id: dict[str, ParsedResource],
) -> str | None:
    source = parsed_by_id.get(result.source_resource_id)
    source_type = source.source_type.lower() if source else ""

    if source_type.startswith("microsoft.keyvault/"):
        return _UNIT_KEYVAULT
    if source_type.startswith("microsoft.web/sites"):
        return _UNIT_FUNCTIONS
    if source_type.startswith("microsoft.network/"):
        return _UNIT_NETWORKING

    target_types = _extract_target_types(result.target_resource_type)
    if any(resource.startswith("AWS::EC2::") for resource in target_types):
        return _UNIT_NETWORKING
    if any(resource.startswith("AWS::Lambda::") for resource in target_types):
        return _UNIT_FUNCTIONS
    if any(
        resource.startswith("AWS::SecretsManager::") or resource.startswith("AWS::KMS::")
        for resource in target_types
    ):
        return _UNIT_KEYVAULT
    return None


def _extract_target_types(raw_target: str) -> list[str]:
    return re.findall(r"AWS::[A-Za-z0-9:]+", raw_target)


def _append_keyvault_resources(
    resources: dict[str, Any],
    outputs: dict[str, Any],
    results: list[MappingResult],
    tags: dict[str, str],
    name_prefix: str,
    default_deletion_policy: str,
    deletion_overrides: dict[str, str],
    default_update_replace_policy: str,
    update_replace_overrides: dict[str, str],
    harden_least_privilege: bool,
) -> None:
    for index, result in enumerate(results, start=1):
        target_types = _extract_target_types(result.target_resource_type)
        include_kms = "AWS::KMS::Key" in target_types or not target_types
        include_secret = "AWS::SecretsManager::Secret" in target_types or not target_types

        kms_logical_id = f"KmsKey{index}"
        secret_logical_id = f"MigratedSecret{index}"

        if include_kms:
            kms_type = "AWS::KMS::Key"
            key_actions = [
                "kms:CreateAlias",
                "kms:Decrypt",
                "kms:DescribeKey",
                "kms:EnableKeyRotation",
                "kms:Encrypt",
                "kms:GenerateDataKey",
                "kms:GetKeyPolicy",
                "kms:ListAliases",
                "kms:PutKeyPolicy",
                "kms:ReEncryptFrom",
                "kms:ReEncryptTo",
            ]
            resources[kms_logical_id] = {
                "Type": kms_type,
                "Properties": {
                    "Description": {"Fn::Sub": "${ProjectName} migrated key material"},
                    "EnableKeyRotation": True,
                    "KeyPolicy": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "AllowAccountRootAdministration",
                                "Effect": "Allow",
                                "Principal": {
                                    "AWS": {
                                        "Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:root"
                                    }
                                },
                                "Action": key_actions,
                                "Resource": {
                                    "Fn::Sub": "arn:${AWS::Partition}:kms:${AWS::Region}:${AWS::AccountId}:key/*"
                                },
                            }
                        ],
                    },
                    "Tags": _standard_tags(tags),
                },
                "DeletionPolicy": deletion_overrides.get(kms_type, default_deletion_policy),
                "UpdateReplacePolicy": update_replace_overrides.get(
                    kms_type,
                    default_update_replace_policy,
                ),
            }
            outputs[f"{kms_logical_id}Arn"] = {
                "Description": "KMS key ARN for migrated secrets",
                "Value": {"Fn::GetAtt": [kms_logical_id, "Arn"]},
                "Export": {"Name": {"Fn::Sub": f"{name_prefix}-keyvault-{kms_logical_id}-arn"}},
            }

        if include_secret:
            secret_type = "AWS::SecretsManager::Secret"
            secret_properties: dict[str, Any] = {
                "Name": {"Fn::Sub": f"{name_prefix}-${{EnvironmentName}}-secret-{index}"},
                "Description": "Placeholder metadata-only migrated secret",
                "Tags": _standard_tags(tags),
            }
            if include_kms:
                secret_properties["KmsKeyId"] = {"Ref": kms_logical_id}

            resources[secret_logical_id] = {
                "Type": secret_type,
                "Properties": secret_properties,
                "DeletionPolicy": deletion_overrides.get(secret_type, default_deletion_policy),
                "UpdateReplacePolicy": update_replace_overrides.get(
                    secret_type,
                    default_update_replace_policy,
                ),
            }
            outputs[f"{secret_logical_id}Arn"] = {
                "Description": "Secrets Manager secret ARN",
                "Value": {"Ref": secret_logical_id},
                "Export": {
                    "Name": {"Fn::Sub": f"{name_prefix}-keyvault-{secret_logical_id}-arn"}
                },
            }


def _append_function_resources(
    resources: dict[str, Any],
    outputs: dict[str, Any],
    results: list[MappingResult],
    tags: dict[str, str],
    name_prefix: str,
    default_deletion_policy: str,
    deletion_overrides: dict[str, str],
    default_update_replace_policy: str,
    update_replace_overrides: dict[str, str],
    networking_required: bool,
    networking_exports: dict[str, str],
    harden_public_access: bool,
) -> None:
    role_logical_id = "FunctionExecutionRole"
    role_type = "AWS::IAM::Role"
    resources[role_logical_id] = {
        "Type": role_type,
        "Properties": {
            "RoleName": {"Fn::Sub": f"{name_prefix}-${{EnvironmentName}}-lambda-role"},
            "AssumeRolePolicyDocument": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": ["lambda.amazonaws.com"]},
                        "Action": ["sts:AssumeRole"],
                    }
                ],
            },
            "ManagedPolicyArns": [
                {
                    "Fn::Sub": (
                        "arn:${AWS::Partition}:iam::aws:policy/service-role/"
                        "AWSLambdaBasicExecutionRole"
                    )
                }
            ],
            "Tags": _standard_tags(tags),
        },
        "DeletionPolicy": deletion_overrides.get(role_type, default_deletion_policy),
        "UpdateReplacePolicy": update_replace_overrides.get(
            role_type,
            default_update_replace_policy,
        ),
    }

    for index, _result in enumerate(results, start=1):
        logical_id = f"MigratedFunction{index}"
        lambda_type = "AWS::Lambda::Function"
        function_properties: dict[str, Any] = {
            "FunctionName": {"Fn::Sub": f"{name_prefix}-${{EnvironmentName}}-fn-{index}"},
            "Role": {"Fn::GetAtt": [role_logical_id, "Arn"]},
            "Runtime": "python3.11",
            "Handler": "index.handler",
            "Code": {
                "ZipFile": (
                    "def handler(event, context):\n"
                    "    return {'statusCode': 200, 'body': 'Migrated function placeholder'}"
                )
            },
            "Timeout": 30,
            "MemorySize": 256,
            "Environment": {
                "Variables": {
                    "MIGRATION_PROJECT": {"Ref": "ProjectName"},
                }
            },
            "Tags": _standard_tags(tags),
        }

        if networking_required:
            function_properties["VpcConfig"] = {
                "SubnetIds": [
                    {"Fn::ImportValue": networking_exports["private_subnet_export"]}
                ],
                "SecurityGroupIds": [{"Ref": "FunctionSecurityGroup"}],
            }

        resources[logical_id] = {
            "Type": lambda_type,
            "Properties": function_properties,
            "DeletionPolicy": deletion_overrides.get(lambda_type, default_deletion_policy),
            "UpdateReplacePolicy": update_replace_overrides.get(
                lambda_type,
                default_update_replace_policy,
            ),
        }

        outputs[f"{logical_id}Arn"] = {
            "Description": "Lambda function ARN",
            "Value": {"Fn::GetAtt": [logical_id, "Arn"]},
            "Export": {"Name": {"Fn::Sub": f"{name_prefix}-functions-{logical_id}-arn"}},
        }

    if networking_required:
        egress_rule: dict[str, Any]
        if harden_public_access:
            egress_rule = {
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "CidrIp": "10.0.0.0/8",
            }
        else:
            egress_rule = {
                "IpProtocol": "-1",
                "CidrIp": "0.0.0.0/0",
            }

        resources["FunctionSecurityGroup"] = {
            "Type": "AWS::EC2::SecurityGroup",
            "Properties": {
                "GroupDescription": "Security group for migrated Lambda functions",
                "VpcId": {"Fn::ImportValue": networking_exports["vpc_export"]},
                "SecurityGroupEgress": [
                    egress_rule
                ],
                "Tags": _standard_tags(tags),
            },
            "DeletionPolicy": deletion_overrides.get("AWS::EC2::SecurityGroup", default_deletion_policy),
            "UpdateReplacePolicy": update_replace_overrides.get(
                "AWS::EC2::SecurityGroup",
                default_update_replace_policy,
            ),
        }


def _append_networking_resources(
    resources: dict[str, Any],
    outputs: dict[str, Any],
    results: list[MappingResult],
    tags: dict[str, str],
    name_prefix: str,
    default_deletion_policy: str,
    deletion_overrides: dict[str, str],
    default_update_replace_policy: str,
    update_replace_overrides: dict[str, str],
    networking_exports: dict[str, str],
) -> None:
    vpc_type = "AWS::EC2::VPC"
    resources["MigratedVpc"] = {
        "Type": vpc_type,
        "Properties": {
            "CidrBlock": "10.10.0.0/16",
            "EnableDnsSupport": True,
            "EnableDnsHostnames": True,
            "Tags": _standard_tags(tags),
        },
        "DeletionPolicy": deletion_overrides.get(vpc_type, default_deletion_policy),
        "UpdateReplacePolicy": update_replace_overrides.get(
            vpc_type,
            default_update_replace_policy,
        ),
    }

    subnet_index = 0
    for result in results:
        target_types = _extract_target_types(result.target_resource_type)
        if "AWS::EC2::Subnet" not in target_types:
            continue

        subnet_index += 1
        logical_id = f"MigratedSubnet{subnet_index}"
        subnet_type = "AWS::EC2::Subnet"
        resources[logical_id] = {
            "Type": subnet_type,
            "Properties": {
                "VpcId": {"Ref": "MigratedVpc"},
                "CidrBlock": f"10.10.{subnet_index}.0/24",
                "MapPublicIpOnLaunch": False,
                "Tags": _standard_tags(tags),
            },
            "DeletionPolicy": deletion_overrides.get(subnet_type, default_deletion_policy),
            "UpdateReplacePolicy": update_replace_overrides.get(
                subnet_type,
                default_update_replace_policy,
            ),
        }

    outputs["VpcId"] = {
        "Description": "VPC identifier",
        "Value": {"Ref": "MigratedVpc"},
        "Export": {"Name": networking_exports["vpc_export"]},
    }

    private_subnet_ref = "MigratedSubnet1" if "MigratedSubnet1" in resources else "MigratedVpc"
    outputs["PrivateSubnetId"] = {
        "Description": "Primary private subnet identifier",
        "Value": {"Ref": private_subnet_ref},
        "Export": {"Name": networking_exports["private_subnet_export"]},
    }


def _networking_export_names(prefix: str) -> dict[str, str]:
    return {
        "vpc_export": {"Fn::Sub": f"{prefix}-networking-vpc-id"},
        "private_subnet_export": {"Fn::Sub": f"{prefix}-networking-private-subnet-id"},
    }


def _standard_tags(tagging_standard: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"Key": "Owner", "Value": tagging_standard.get("owner", "platform-team")},
        {
            "Key": "CostCenter",
            "Value": tagging_standard.get("cost_center", "engineering"),
        },
        {
            "Key": "DataClassification",
            "Value": tagging_standard.get("data_classification", "internal"),
        },
        {
            "Key": "ManagedBy",
            "Value": tagging_standard.get("managed_by", "migration-assistant"),
        },
        {
            "Key": "MigrationProject",
            "Value": tagging_standard.get("migration_project", "azure-to-aws"),
        },
    ]


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _feedback_mentions(findings: list[ValidationFinding], terms: tuple[str, ...]) -> bool:
    lowered_terms = tuple(term.lower() for term in terms)
    for finding in findings:
        haystack = f"{finding.check_id} {finding.message}".lower()
        if any(term in haystack for term in lowered_terms):
            return True
    return False
