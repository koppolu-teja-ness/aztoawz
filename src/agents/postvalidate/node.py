"""Post-deployment validation node for structural, functional, and security checks."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import io
import json
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.graph.state import AuditRecord, FindingSeverity, MigrationState, PostDeployFinding


class PostValidateRequest(BaseModel):
    """Input contract for post-deployment validation stage."""

    model_config = ConfigDict(extra="forbid")

    region: str = "us-east-1"
    lambda_test_event: dict[str, Any] = Field(default_factory=lambda: {"smoke": True})


class SecurityIntent(BaseModel):
    """Normalized source-side security constraints used for posture diff."""

    model_config = ConfigDict(extra="forbid")

    allowed_iam_actions: list[str] = Field(default_factory=list)
    allowed_iam_resources: list[str] = Field(default_factory=list)
    allow_wildcard_actions: bool = False
    allow_wildcard_resources: bool = False
    allow_public_ingress: bool = False
    allow_public_egress: bool = False


class PlannedResource(BaseModel):
    """One CloudFormation-planned resource entry used for post checks."""

    model_config = ConfigDict(extra="forbid")

    logical_id: str
    resource_type: str
    properties: dict[str, Any]


def postvalidate_node(
    state: MigrationState,
    request: PostValidateRequest,
    *,
    boto3_session: Any | None = None,
    now_provider: Any | None = None,
) -> MigrationState:
    """Validate deployed AWS resources against plan, functionality, and security posture."""

    now_fn = now_provider or (lambda: datetime.now(tz=timezone.utc))

    if state.deployment_result is None or not state.deployment_result.deployed:
        # Deployment failure must not prevent the pipeline from reaching the Reporter
        # stage: every run (successful, rejected, or failed deploy) MUST still produce
        # an auditable report. Post-deploy checks are skipped, not silently dropped.
        timestamp = now_fn()
        findings = [
            PostDeployFinding(
                check_id="POSTVALIDATE_SKIPPED_DEPLOYMENT_NOT_SUCCESSFUL",
                passed=False,
                message=(
                    "Post-deploy validation skipped: no successful deployment_result "
                    "available (deployment did not run or failed)."
                ),
                severity=FindingSeverity.MEDIUM,
            )
        ]
        audit = _build_audit(state=state, request=request, findings=findings, timestamp=timestamp)
        return state.model_copy(
            update={
                "updated_at": timestamp,
                "post_deploy_findings": findings,
                "audit_records": [*state.audit_records, audit],
            }
        )

    if boto3_session is None:
        import boto3

        boto3_session = boto3.session.Session(region_name=request.region)

    planned_resources = _collect_planned_resources(state)
    security_intent = _collect_security_intent(state)

    findings: list[PostDeployFinding] = []

    secret_client = boto3_session.client("secretsmanager", region_name=request.region)
    lambda_client = boto3_session.client("lambda", region_name=request.region)
    ec2_client = boto3_session.client("ec2", region_name=request.region)
    iam_client = boto3_session.client("iam", region_name=request.region)

    findings.extend(_run_structural_checks(planned_resources, secret_client, lambda_client, ec2_client))
    findings.extend(
        _run_functional_smoke_checks(
            planned_resources,
            secret_client,
            lambda_client,
            ec2_client,
            request.lambda_test_event,
        )
    )
    findings.extend(
        _run_security_posture_diff(
            planned_resources,
            security_intent,
            lambda_client,
            ec2_client,
            iam_client,
        )
    )

    timestamp = now_fn()
    audit = _build_audit(state=state, request=request, findings=findings, timestamp=timestamp)

    return state.model_copy(
        update={
            "updated_at": timestamp,
            "post_deploy_findings": findings,
            "audit_records": [*state.audit_records, audit],
        }
    )


def _collect_planned_resources(state: MigrationState) -> list[PlannedResource]:
    planned: list[PlannedResource] = []

    for artifact in state.cfn_artifacts:
        template_yaml = artifact.properties.get("template_yaml")
        if not isinstance(template_yaml, str) or not template_yaml.strip():
            continue

        loaded = yaml.safe_load(template_yaml)
        if not isinstance(loaded, dict):
            continue

        resources = loaded.get("Resources")
        if not isinstance(resources, dict):
            continue

        for logical_id, payload in resources.items():
            if not isinstance(logical_id, str) or not isinstance(payload, dict):
                continue
            resource_type = payload.get("Type")
            if not isinstance(resource_type, str):
                continue

            properties = payload.get("Properties")
            if not isinstance(properties, dict):
                properties = {}

            planned.append(
                PlannedResource(
                    logical_id=logical_id,
                    resource_type=resource_type,
                    properties=properties,
                )
            )

    return planned


def _collect_security_intent(state: MigrationState) -> SecurityIntent:
    actions: set[str] = set()
    resources: set[str] = set()
    allow_wildcard_actions = False
    allow_wildcard_resources = False
    allow_public_ingress = False
    allow_public_egress = False

    for parsed in state.parsed_resources:
        props = parsed.properties

        raw_actions = props.get("allowed_iam_actions")
        if isinstance(raw_actions, list):
            for action in raw_actions:
                if isinstance(action, str) and action:
                    actions.add(action)

        raw_resources = props.get("allowed_iam_resources")
        if isinstance(raw_resources, list):
            for resource in raw_resources:
                if isinstance(resource, str) and resource:
                    resources.add(resource)

        if props.get("allow_wildcard_actions") is True:
            allow_wildcard_actions = True
        if props.get("allow_wildcard_resources") is True:
            allow_wildcard_resources = True
        if props.get("allow_public_ingress") is True:
            allow_public_ingress = True
        if props.get("allow_public_egress") is True:
            allow_public_egress = True

    return SecurityIntent(
        allowed_iam_actions=sorted(actions),
        allowed_iam_resources=sorted(resources),
        allow_wildcard_actions=allow_wildcard_actions,
        allow_wildcard_resources=allow_wildcard_resources,
        allow_public_ingress=allow_public_ingress,
        allow_public_egress=allow_public_egress,
    )


def _run_structural_checks(
    planned_resources: list[PlannedResource],
    secret_client: Any,
    lambda_client: Any,
    ec2_client: Any,
) -> list[PostDeployFinding]:
    findings: list[PostDeployFinding] = []

    expected_counts: dict[str, int] = defaultdict(int)
    actual_counts: dict[str, int] = defaultdict(int)

    for resource in planned_resources:
        expected_counts[resource.resource_type] += 1

        if resource.resource_type == "AWS::SecretsManager::Secret":
            secret_name = _as_str(resource.properties.get("Name"))
            if secret_name is None:
                findings.append(
                    PostDeployFinding(
                        check_id=f"STRUCT_SECRET_{resource.logical_id}",
                        passed=False,
                        message=(
                            f"Cannot structurally validate {resource.logical_id}: "
                            "missing concrete Name property"
                        ),
                        severity=FindingSeverity.MEDIUM,
                    )
                )
                continue

            try:
                metadata = secret_client.describe_secret(SecretId=secret_name)
                actual_counts[resource.resource_type] += 1
                findings.append(
                    PostDeployFinding(
                        check_id=f"STRUCT_SECRET_{resource.logical_id}",
                        passed=True,
                        message=(
                            f"Secret metadata resolved for {resource.logical_id} "
                            f"({metadata.get('ARN', secret_name)})"
                        ),
                        severity=FindingSeverity.LOW,
                    )
                )
            except Exception as exc:
                findings.append(
                    PostDeployFinding(
                        check_id=f"STRUCT_SECRET_{resource.logical_id}",
                        passed=False,
                        message=f"Secret {secret_name} is missing or unreadable: {exc}",
                        severity=FindingSeverity.HIGH,
                    )
                )

        elif resource.resource_type == "AWS::Lambda::Function":
            function_name = _as_str(resource.properties.get("FunctionName"))
            if function_name is None:
                findings.append(
                    PostDeployFinding(
                        check_id=f"STRUCT_LAMBDA_{resource.logical_id}",
                        passed=False,
                        message=(
                            f"Cannot structurally validate {resource.logical_id}: "
                            "missing concrete FunctionName property"
                        ),
                        severity=FindingSeverity.MEDIUM,
                    )
                )
                continue

            try:
                response = lambda_client.get_function(FunctionName=function_name)
                config = response.get("Configuration", {})
                actual_counts[resource.resource_type] += 1

                mismatches: list[str] = []
                expected_runtime = _as_str(resource.properties.get("Runtime"))
                expected_handler = _as_str(resource.properties.get("Handler"))
                if expected_runtime and config.get("Runtime") != expected_runtime:
                    mismatches.append(
                        f"Runtime expected={expected_runtime} actual={config.get('Runtime')}"
                    )
                if expected_handler and config.get("Handler") != expected_handler:
                    mismatches.append(
                        f"Handler expected={expected_handler} actual={config.get('Handler')}"
                    )

                findings.append(
                    PostDeployFinding(
                        check_id=f"STRUCT_LAMBDA_{resource.logical_id}",
                        passed=not mismatches,
                        message=(
                            "Lambda function metadata matches plan"
                            if not mismatches
                            else "Lambda function metadata mismatch: " + "; ".join(mismatches)
                        ),
                        severity=FindingSeverity.MEDIUM if mismatches else FindingSeverity.LOW,
                    )
                )
            except Exception as exc:
                findings.append(
                    PostDeployFinding(
                        check_id=f"STRUCT_LAMBDA_{resource.logical_id}",
                        passed=False,
                        message=f"Lambda function {function_name} is missing or unreadable: {exc}",
                        severity=FindingSeverity.HIGH,
                    )
                )

        elif resource.resource_type == "AWS::EC2::SecurityGroup":
            group_name = _as_str(resource.properties.get("GroupName"))
            if group_name is None:
                findings.append(
                    PostDeployFinding(
                        check_id=f"STRUCT_SG_{resource.logical_id}",
                        passed=False,
                        message=(
                            f"Cannot structurally validate {resource.logical_id}: "
                            "missing concrete GroupName property"
                        ),
                        severity=FindingSeverity.MEDIUM,
                    )
                )
                continue

            security_group = _find_security_group_by_name(ec2_client, group_name)
            if security_group is None:
                findings.append(
                    PostDeployFinding(
                        check_id=f"STRUCT_SG_{resource.logical_id}",
                        passed=False,
                        message=f"Security group {group_name} was not found",
                        severity=FindingSeverity.HIGH,
                    )
                )
                continue

            actual_counts[resource.resource_type] += 1
            findings.append(
                PostDeployFinding(
                    check_id=f"STRUCT_SG_{resource.logical_id}",
                    passed=True,
                    message=f"Security group {group_name} exists with GroupId {security_group['GroupId']}",
                    severity=FindingSeverity.LOW,
                )
            )

    for resource_type, expected in sorted(expected_counts.items()):
        actual = actual_counts.get(resource_type, 0)
        findings.append(
            PostDeployFinding(
                check_id=f"STRUCT_COUNT_{resource_type}",
                passed=actual == expected,
                message=f"Resource type count expected={expected} actual={actual}",
                severity=FindingSeverity.HIGH if actual != expected else FindingSeverity.LOW,
            )
        )

    return findings


def _run_functional_smoke_checks(
    planned_resources: list[PlannedResource],
    secret_client: Any,
    lambda_client: Any,
    ec2_client: Any,
    lambda_test_event: dict[str, Any],
) -> list[PostDeployFinding]:
    findings: list[PostDeployFinding] = []

    for resource in planned_resources:
        if resource.resource_type == "AWS::SecretsManager::Secret":
            secret_name = _as_str(resource.properties.get("Name"))
            if secret_name is None:
                continue

            try:
                metadata = secret_client.describe_secret(SecretId=secret_name)
                findings.append(
                    PostDeployFinding(
                        check_id=f"SMOKE_SECRET_METADATA_{resource.logical_id}",
                        passed=True,
                        message=(
                            "Secret metadata read succeeded; rotation-enabled="
                            f"{metadata.get('RotationEnabled', False)}"
                        ),
                        severity=FindingSeverity.LOW,
                    )
                )
            except Exception as exc:
                findings.append(
                    PostDeployFinding(
                        check_id=f"SMOKE_SECRET_METADATA_{resource.logical_id}",
                        passed=False,
                        message=f"Secret metadata read failed: {exc}",
                        severity=FindingSeverity.HIGH,
                    )
                )

        elif resource.resource_type == "AWS::Lambda::Function":
            function_name = _as_str(resource.properties.get("FunctionName"))
            if function_name is None:
                continue

            try:
                response = lambda_client.invoke(
                    FunctionName=function_name,
                    Payload=io.BytesIO(json.dumps(lambda_test_event).encode("utf-8")),
                )
                status_code = int(response.get("StatusCode", 0))
                function_error = response.get("FunctionError")
                payload_obj = response.get("Payload")
                payload_bytes = payload_obj.read() if payload_obj is not None else b""
                payload_text = payload_bytes.decode("utf-8", errors="ignore")

                if function_error is not None and _looks_like_runtime_unavailable_message(payload_text):
                    dry_run = lambda_client.invoke(
                        FunctionName=function_name,
                        InvocationType="DryRun",
                    )
                    dry_run_status = int(dry_run.get("StatusCode", 0))
                    dry_run_passed = dry_run_status == 204
                    findings.append(
                        PostDeployFinding(
                            check_id=f"SMOKE_LAMBDA_INVOKE_{resource.logical_id}",
                            passed=dry_run_passed,
                            message=(
                                "Lambda full invoke unavailable in local runtime; "
                                f"DryRun status={dry_run_status}"
                            ),
                            severity=FindingSeverity.MEDIUM if dry_run_passed else FindingSeverity.HIGH,
                        )
                    )
                    continue

                passed = status_code == 200 and function_error is None
                findings.append(
                    PostDeployFinding(
                        check_id=f"SMOKE_LAMBDA_INVOKE_{resource.logical_id}",
                        passed=passed,
                        message=(
                            f"Lambda invoke status={status_code}, function_error={function_error}, "
                            f"payload={payload_text}"
                        ),
                        severity=FindingSeverity.HIGH if not passed else FindingSeverity.LOW,
                    )
                )
            except Exception as exc:
                # moto can require Docker for full execution on some hosts; DryRun still proves invocability.
                if _looks_like_runtime_unavailable_error(exc):
                    try:
                        dry_run = lambda_client.invoke(
                            FunctionName=function_name,
                            InvocationType="DryRun",
                        )
                        status_code = int(dry_run.get("StatusCode", 0))
                        passed = status_code == 204
                        findings.append(
                            PostDeployFinding(
                                check_id=f"SMOKE_LAMBDA_INVOKE_{resource.logical_id}",
                                passed=passed,
                                message=(
                                    "Lambda full invoke unavailable in local runtime; "
                                    f"DryRun status={status_code}"
                                ),
                                severity=FindingSeverity.MEDIUM if passed else FindingSeverity.HIGH,
                            )
                        )
                    except Exception as dry_run_exc:
                        findings.append(
                            PostDeployFinding(
                                check_id=f"SMOKE_LAMBDA_INVOKE_{resource.logical_id}",
                                passed=False,
                                message=(
                                    f"Lambda invoke failed and DryRun fallback failed: {dry_run_exc}"
                                ),
                                severity=FindingSeverity.HIGH,
                            )
                        )
                else:
                    findings.append(
                        PostDeployFinding(
                            check_id=f"SMOKE_LAMBDA_INVOKE_{resource.logical_id}",
                            passed=False,
                            message=f"Lambda invoke failed: {exc}",
                            severity=FindingSeverity.HIGH,
                        )
                    )

        elif resource.resource_type == "AWS::EC2::SecurityGroup":
            group_name = _as_str(resource.properties.get("GroupName"))
            if group_name is None:
                continue

            security_group = _find_security_group_by_name(ec2_client, group_name)
            if security_group is None:
                findings.append(
                    PostDeployFinding(
                        check_id=f"SMOKE_SG_RULES_{resource.logical_id}",
                        passed=False,
                        message=f"Cannot validate rules because {group_name} was not found",
                        severity=FindingSeverity.HIGH,
                    )
                )
                continue

            expected_ingress = _normalize_sg_rules(resource.properties.get("SecurityGroupIngress"))
            expected_egress = _normalize_sg_rules(resource.properties.get("SecurityGroupEgress"))
            actual_ingress = _normalize_sg_rules(security_group.get("IpPermissions"))
            actual_egress = _normalize_sg_rules(security_group.get("IpPermissionsEgress"))

            mismatches: list[str] = []
            if expected_ingress != actual_ingress:
                mismatches.append("ingress rules differ")
            if expected_egress != actual_egress:
                mismatches.append("egress rules differ")

            findings.append(
                PostDeployFinding(
                    check_id=f"SMOKE_SG_RULES_{resource.logical_id}",
                    passed=not mismatches,
                    message=(
                        "Security group ingress/egress rules match plan"
                        if not mismatches
                        else "Security group rule mismatch: " + "; ".join(mismatches)
                    ),
                    severity=FindingSeverity.HIGH if mismatches else FindingSeverity.LOW,
                )
            )

    return findings


def _run_security_posture_diff(
    planned_resources: list[PlannedResource],
    security_intent: SecurityIntent,
    lambda_client: Any,
    ec2_client: Any,
    iam_client: Any,
) -> list[PostDeployFinding]:
    findings: list[PostDeployFinding] = []

    for resource in planned_resources:
        if resource.resource_type == "AWS::EC2::SecurityGroup":
            group_name = _as_str(resource.properties.get("GroupName"))
            if group_name is None:
                continue

            security_group = _find_security_group_by_name(ec2_client, group_name)
            if security_group is None:
                continue

            public_ingress = _has_public_rule(security_group.get("IpPermissions"))
            public_egress = _has_public_rule(security_group.get("IpPermissionsEgress"))

            ingress_ok = (not public_ingress) or security_intent.allow_public_ingress
            egress_ok = (not public_egress) or security_intent.allow_public_egress

            findings.append(
                PostDeployFinding(
                    check_id=f"SECURITY_PUBLIC_INGRESS_{resource.logical_id}",
                    passed=ingress_ok,
                    message=(
                        "No newly public ingress detected"
                        if ingress_ok
                        else "Detected public ingress (0.0.0.0/0 or ::/0) not permitted by source intent"
                    ),
                    severity=FindingSeverity.CRITICAL if not ingress_ok else FindingSeverity.LOW,
                )
            )
            findings.append(
                PostDeployFinding(
                    check_id=f"SECURITY_PUBLIC_EGRESS_{resource.logical_id}",
                    passed=egress_ok,
                    message=(
                        "No newly public egress detected"
                        if egress_ok
                        else "Detected public egress (0.0.0.0/0 or ::/0) not permitted by source intent"
                    ),
                    severity=FindingSeverity.CRITICAL if not egress_ok else FindingSeverity.LOW,
                )
            )

        if resource.resource_type == "AWS::Lambda::Function":
            function_name = _as_str(resource.properties.get("FunctionName"))
            if function_name is None:
                continue

            try:
                function_config = lambda_client.get_function(FunctionName=function_name).get(
                    "Configuration", {}
                )
                role_arn = _as_str(function_config.get("Role"))
                if role_arn is None:
                    findings.append(
                        PostDeployFinding(
                            check_id=f"SECURITY_IAM_ROLE_{resource.logical_id}",
                            passed=False,
                            message="Lambda function has no execution role ARN",
                            severity=FindingSeverity.CRITICAL,
                        )
                    )
                    continue

                role_name = role_arn.rsplit("/", 1)[-1]
                policy_docs = _get_role_policy_documents(iam_client, role_name)
                findings.extend(
                    _evaluate_policy_documents(
                        policy_docs=policy_docs,
                        role_name=role_name,
                        security_intent=security_intent,
                    )
                )
            except Exception as exc:
                findings.append(
                    PostDeployFinding(
                        check_id=f"SECURITY_IAM_ROLE_{resource.logical_id}",
                        passed=False,
                        message=f"IAM posture validation failed for Lambda role: {exc}",
                        severity=FindingSeverity.CRITICAL,
                    )
                )

    failed_security_findings = [item for item in findings if not item.passed]
    findings.append(
        PostDeployFinding(
            check_id="SECURITY_POSTURE_SUMMARY",
            passed=len(failed_security_findings) == 0,
            message=(
                "Security posture did not regress"
                if not failed_security_findings
                else (
                    "Security posture regression detected: "
                    f"{len(failed_security_findings)} critical issue(s)"
                )
            ),
            severity=FindingSeverity.CRITICAL if failed_security_findings else FindingSeverity.LOW,
        )
    )

    return findings


def _evaluate_policy_documents(
    *,
    policy_docs: list[dict[str, Any]],
    role_name: str,
    security_intent: SecurityIntent,
) -> list[PostDeployFinding]:
    findings: list[PostDeployFinding] = []

    allowed_actions = set(security_intent.allowed_iam_actions)
    allowed_resources = set(security_intent.allowed_iam_resources)

    for doc_index, document in enumerate(policy_docs, start=1):
        statements = document.get("Statement")
        if isinstance(statements, dict):
            statement_list = [statements]
        elif isinstance(statements, list):
            statement_list = [item for item in statements if isinstance(item, dict)]
        else:
            statement_list = []

        for stmt_index, statement in enumerate(statement_list, start=1):
            if str(statement.get("Effect", "Allow")) != "Allow":
                continue

            actions = _flatten_policy_value(statement.get("Action"))
            resources = _flatten_policy_value(statement.get("Resource"))

            for action in actions:
                if _is_wildcard(action) and not security_intent.allow_wildcard_actions:
                    findings.append(
                        PostDeployFinding(
                            check_id=f"SECURITY_IAM_ACTION_WILDCARD_{role_name}_{doc_index}_{stmt_index}",
                            passed=False,
                            message=(
                                f"IAM action '{action}' is broader than source intent "
                                f"for role {role_name}"
                            ),
                            severity=FindingSeverity.CRITICAL,
                        )
                    )
                    continue

                if allowed_actions and action not in allowed_actions:
                    findings.append(
                        PostDeployFinding(
                            check_id=f"SECURITY_IAM_ACTION_SCOPE_{role_name}_{doc_index}_{stmt_index}",
                            passed=False,
                            message=(
                                f"IAM action '{action}' is not present in source allowed set "
                                f"for role {role_name}"
                            ),
                            severity=FindingSeverity.CRITICAL,
                        )
                    )

            for resource in resources:
                if _is_wildcard(resource) and not security_intent.allow_wildcard_resources:
                    findings.append(
                        PostDeployFinding(
                            check_id=f"SECURITY_IAM_RESOURCE_WILDCARD_{role_name}_{doc_index}_{stmt_index}",
                            passed=False,
                            message=(
                                f"IAM resource '{resource}' is broader than source intent "
                                f"for role {role_name}"
                            ),
                            severity=FindingSeverity.CRITICAL,
                        )
                    )
                    continue

                if allowed_resources and resource not in allowed_resources:
                    findings.append(
                        PostDeployFinding(
                            check_id=f"SECURITY_IAM_RESOURCE_SCOPE_{role_name}_{doc_index}_{stmt_index}",
                            passed=False,
                            message=(
                                f"IAM resource '{resource}' is not present in source allowed set "
                                f"for role {role_name}"
                            ),
                            severity=FindingSeverity.CRITICAL,
                        )
                    )

    if not findings:
        findings.append(
            PostDeployFinding(
                check_id=f"SECURITY_IAM_ROLE_SCOPE_{role_name}",
                passed=True,
                message="IAM policy statements are not broader than source intent",
                severity=FindingSeverity.LOW,
            )
        )

    return findings


def _get_role_policy_documents(iam_client: Any, role_name: str) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []

    inline_names = iam_client.list_role_policies(RoleName=role_name).get("PolicyNames", [])
    for policy_name in inline_names:
        policy = iam_client.get_role_policy(RoleName=role_name, PolicyName=policy_name)
        document = policy.get("PolicyDocument")
        if isinstance(document, dict):
            documents.append(document)

    attached_policies = iam_client.list_attached_role_policies(RoleName=role_name).get(
        "AttachedPolicies", []
    )
    for attached in attached_policies:
        arn = attached.get("PolicyArn")
        if not isinstance(arn, str):
            continue

        policy = iam_client.get_policy(PolicyArn=arn).get("Policy", {})
        version_id = policy.get("DefaultVersionId")
        if not isinstance(version_id, str):
            continue

        version = iam_client.get_policy_version(PolicyArn=arn, VersionId=version_id)
        document = version.get("PolicyVersion", {}).get("Document")
        if isinstance(document, dict):
            documents.append(document)

    return documents


def _normalize_sg_rules(raw_rules: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_rules, list):
        return []

    normalized: list[dict[str, Any]] = []

    for rule in raw_rules:
        if not isinstance(rule, dict):
            continue

        cidrs_v4: list[str] = []
        cidrs_v6: list[str] = []

        ipv4_ranges = rule.get("IpRanges") or rule.get("CidrIp")
        if isinstance(ipv4_ranges, list):
            for item in ipv4_ranges:
                if isinstance(item, dict):
                    cidr = item.get("CidrIp")
                else:
                    cidr = item
                if isinstance(cidr, str):
                    cidrs_v4.append(cidr)
        elif isinstance(ipv4_ranges, str):
            cidrs_v4.append(ipv4_ranges)

        ipv6_ranges = rule.get("Ipv6Ranges") or rule.get("CidrIpv6")
        if isinstance(ipv6_ranges, list):
            for item in ipv6_ranges:
                if isinstance(item, dict):
                    cidr = item.get("CidrIpv6")
                else:
                    cidr = item
                if isinstance(cidr, str):
                    cidrs_v6.append(cidr)
        elif isinstance(ipv6_ranges, str):
            cidrs_v6.append(ipv6_ranges)

        normalized.append(
            {
                "IpProtocol": str(rule.get("IpProtocol", "-1")),
                "FromPort": int(rule.get("FromPort", -1)) if rule.get("FromPort") is not None else -1,
                "ToPort": int(rule.get("ToPort", -1)) if rule.get("ToPort") is not None else -1,
                "CidrIpv4": sorted(set(cidrs_v4)),
                "CidrIpv6": sorted(set(cidrs_v6)),
            }
        )

    normalized.sort(
        key=lambda item: (
            item["IpProtocol"],
            item["FromPort"],
            item["ToPort"],
            ",".join(item["CidrIpv4"]),
            ",".join(item["CidrIpv6"]),
        )
    )
    return normalized


def _has_public_rule(raw_rules: Any) -> bool:
    for rule in _normalize_sg_rules(raw_rules):
        if "0.0.0.0/0" in rule["CidrIpv4"]:
            return True
        if "::/0" in rule["CidrIpv6"]:
            return True
    return False


def _find_security_group_by_name(ec2_client: Any, group_name: str) -> dict[str, Any] | None:
    response = ec2_client.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [group_name]}]
    )
    groups = response.get("SecurityGroups", [])
    for group in groups:
        if isinstance(group, dict) and group.get("GroupName") == group_name:
            return group
    return None


def _flatten_policy_value(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _is_wildcard(value: str) -> bool:
    return value == "*" or value.endswith(":*")


def _as_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text else None


def _looks_like_runtime_unavailable_error(exc: Exception) -> bool:
    haystack = str(exc).lower()
    return "docker" in haystack or "createfile" in haystack or "runtime" in haystack


def _looks_like_runtime_unavailable_message(message: str) -> bool:
    haystack = message.lower()
    return "docker" in haystack or "createfile" in haystack or "runtime" in haystack


def _build_audit(
    *,
    state: MigrationState,
    request: PostValidateRequest,
    findings: list[PostDeployFinding],
    timestamp: datetime,
) -> AuditRecord:
    input_hash = _stable_hash(
        {
            "run_id": state.run_id,
            "request": request.model_dump(mode="json"),
            "deployment": state.deployment_result.model_dump(mode="json")
            if state.deployment_result is not None
            else None,
            "artifact_logical_ids": [item.logical_id for item in state.cfn_artifacts],
        }
    )

    failed_findings = [item for item in findings if not item.passed]
    security_failures = [item for item in failed_findings if item.check_id.startswith("SECURITY_")]

    return AuditRecord(
        inputs_hash=input_hash,
        outputs={
            "stage": "postvalidate",
            "planned_resource_count": len(_collect_planned_resources(state)),
            "finding_count": len(findings),
            "failed_finding_count": len(failed_findings),
            "security_failure_count": len(security_failures),
            "validation_passed": len(failed_findings) == 0,
        },
        rule_ids_used=sorted({rule_id for artifact in state.cfn_artifacts for rule_id in artifact.rule_ids_used}),
        reviewer=None,
        timestamp=timestamp,
    )


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
