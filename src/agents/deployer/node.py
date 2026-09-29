"""Deployment agent node for approved CloudFormation execution with rollback safeguards."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import time
from typing import Any

from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field

from src.graph.state import ApprovalDecision, AuditRecord, CfnArtifact, DeploymentResult, MigrationState


class DeploymentRequest(BaseModel):
    """Input contract for deployment stage execution."""

    model_config = ConfigDict(extra="forbid")

    region: str = "us-east-1"
    max_poll_attempts: int = Field(default=120, ge=1, le=3600)
    poll_interval_seconds: float = Field(default=1.0, ge=0.0, le=30.0)


NowCallable = Callable[[], datetime]
SleepCallable = Callable[[float], None]

_SUCCESS_STACK_STATUSES = {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
_FAILURE_STACK_STATUSES = {
    "CREATE_FAILED",
    "DELETE_FAILED",
    "REVIEW_IN_PROGRESS",
    "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED",
    "UPDATE_FAILED",
    "UPDATE_ROLLBACK_COMPLETE",
    "UPDATE_ROLLBACK_FAILED",
}
_UPDATE_IN_PROGRESS_STATUSES = {
    "UPDATE_IN_PROGRESS",
    "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
    "UPDATE_ROLLBACK_IN_PROGRESS",
    "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
}


def deployer_node(
    state: MigrationState,
    request: DeploymentRequest,
    *,
    cloudformation_client: Any | None = None,
    now_provider: NowCallable | None = None,
    sleep_provider: SleepCallable | None = None,
) -> MigrationState:
    """Deploy approved CloudFormation stacks, halt on failures, and execute rollback procedure."""

    _require_valid_approval(state)

    now_fn = now_provider or (lambda: datetime.now(tz=timezone.utc))
    sleep_fn = sleep_provider or time.sleep

    client = cloudformation_client
    if client is None:
        import boto3

        client = boto3.client("cloudformation", region_name=request.region)

    if not state.cfn_artifacts:
        timestamp = now_fn()
        result = DeploymentResult(
            deployed=True,
            message="No CloudFormation artifacts to deploy.",
            timestamp=timestamp,
        )
        audit = _build_audit_record(
            state=state,
            request=request,
            result=result,
            attempted_stacks=[],
            deployed_stacks=[],
            timestamp=timestamp,
        )
        return state.model_copy(
            update={
                "updated_at": timestamp,
                "deployment_result": result,
                "audit_records": [*state.audit_records, audit],
            }
        )

    ordered_artifacts = _order_artifacts_by_dependency(state.cfn_artifacts)

    attempted_stacks: list[str] = []
    deployed_stacks: list[str] = []
    stack_statuses: dict[str, str] = {}
    resource_statuses: dict[str, dict[str, str]] = {}
    merged_outputs: dict[str, str] = {}
    last_stack_id: str | None = None

    for artifact in ordered_artifacts:
        stack_name = _artifact_stack_name(artifact)
        attempted_stacks.append(stack_name)

        try:
            operation = _resolve_operation(client, stack_name)
            stack_id = _start_stack_operation(client, artifact, stack_name, operation)
            if stack_id is not None:
                last_stack_id = stack_id

            final_status, latest_resource_statuses, outputs = _poll_stack_until_terminal(
                client=client,
                stack_name=stack_name,
                max_attempts=request.max_poll_attempts,
                poll_interval_seconds=request.poll_interval_seconds,
                sleep_provider=sleep_fn,
            )
            stack_statuses[stack_name] = final_status
            resource_statuses[stack_name] = latest_resource_statuses
            merged_outputs.update(outputs)

            if final_status not in _SUCCESS_STACK_STATUSES:
                raise RuntimeError(
                    f"Stack {stack_name} ended in terminal status {final_status}"
                )

            deployed_stacks.append(stack_name)
        except Exception as exc:
            try:
                rollback_actions = _rollback_stacks(
                    client=client,
                    failed_stack_name=stack_name,
                    successful_stack_names=deployed_stacks,
                    max_attempts=request.max_poll_attempts,
                    poll_interval_seconds=request.poll_interval_seconds,
                    sleep_provider=sleep_fn,
                )
                rollback_error: str | None = None
            except Exception as rollback_exc:
                # Rollback itself failing must not crash the pipeline: every deploy
                # attempt (success, failure, or failed rollback) MUST still yield an
                # auditable, reportable DeploymentResult per the idempotent/checkpointed
                # stage contract.
                rollback_actions = []
                rollback_error = str(rollback_exc)

            timestamp = now_fn()
            message = (
                f"Deployment halted after failure in stack {stack_name}: {exc}. "
                "Rollback procedure executed."
            )
            if rollback_error is not None:
                message += f" Rollback also failed: {rollback_error}."
            result = DeploymentResult(
                deployed=False,
                stack_name=stack_name,
                stack_id=last_stack_id,
                outputs=merged_outputs,
                stack_statuses=stack_statuses,
                resource_statuses=resource_statuses,
                rollback_triggered=True,
                rollback_actions=rollback_actions,
                failed_stack=stack_name,
                message=message,
                timestamp=timestamp,
            )
            audit = _build_audit_record(
                state=state,
                request=request,
                result=result,
                attempted_stacks=attempted_stacks,
                deployed_stacks=deployed_stacks,
                timestamp=timestamp,
            )
            return state.model_copy(
                update={
                    "updated_at": timestamp,
                    "deployment_result": result,
                    "audit_records": [*state.audit_records, audit],
                }
            )

    timestamp = now_fn()
    last_stack_name = deployed_stacks[-1] if deployed_stacks else None
    result = DeploymentResult(
        deployed=True,
        stack_name=last_stack_name,
        stack_id=last_stack_id,
        outputs=merged_outputs,
        stack_statuses=stack_statuses,
        resource_statuses=resource_statuses,
        rollback_triggered=False,
        rollback_actions=[],
        failed_stack=None,
        message=f"Successfully deployed {len(deployed_stacks)} stack(s).",
        timestamp=timestamp,
    )
    audit = _build_audit_record(
        state=state,
        request=request,
        result=result,
        attempted_stacks=attempted_stacks,
        deployed_stacks=deployed_stacks,
        timestamp=timestamp,
    )
    return state.model_copy(
        update={
            "updated_at": timestamp,
            "deployment_result": result,
            "audit_records": [*state.audit_records, audit],
        }
    )


def _require_valid_approval(state: MigrationState) -> None:
    approval = state.approval_record
    if state.plan_hash is None:
        raise RuntimeError("Deployment requires state.plan_hash from planner stage")
    if approval is None:
        raise RuntimeError("Deployment refused: missing approval artifact")
    if approval.plan_hash != state.plan_hash:
        raise RuntimeError("Deployment refused: approval artifact plan_hash mismatch")
    if approval.decision != ApprovalDecision.APPROVE:
        raise RuntimeError("Deployment refused: approval decision must be APPROVE")


def _artifact_stack_name(artifact: CfnArtifact) -> str:
    name = artifact.properties.get("stack_name")
    if isinstance(name, str) and name.strip():
        return name
    return artifact.logical_id


def _order_artifacts_by_dependency(artifacts: list[CfnArtifact]) -> list[CfnArtifact]:
    if len(artifacts) <= 1:
        return artifacts

    by_name = {_artifact_stack_name(artifact): artifact for artifact in artifacts}
    dependencies: dict[str, set[str]] = {name: set() for name in by_name}

    for name, artifact in by_name.items():
        raw_depends = artifact.properties.get("depends_on_stacks")
        if isinstance(raw_depends, list):
            for dependency in raw_depends:
                if isinstance(dependency, str) and dependency in by_name and dependency != name:
                    dependencies[name].add(dependency)

    if all(not deps for deps in dependencies.values()):
        unit_priority = {"networking": 0, "keyvault": 1, "functions": 2}
        return sorted(
            artifacts,
            key=lambda artifact: (
                unit_priority.get(str(artifact.properties.get("unit", "other")), 3),
                _artifact_stack_name(artifact).lower(),
            ),
        )

    dependents: dict[str, set[str]] = defaultdict(set)
    indegree: dict[str, int] = {name: len(deps) for name, deps in dependencies.items()}

    for stack_name, deps in dependencies.items():
        for dep in deps:
            dependents[dep].add(stack_name)

    available = [name for name, count in indegree.items() if count == 0]
    ordered_names: list[str] = []

    while available:
        available.sort(key=str.lower)
        current = available.pop(0)
        ordered_names.append(current)

        for dependent in sorted(dependents[current], key=str.lower):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                available.append(dependent)

    if len(ordered_names) != len(by_name):
        remaining = [name for name in by_name if name not in set(ordered_names)]
        remaining.sort(key=str.lower)
        ordered_names.extend(remaining)

    return [by_name[name] for name in ordered_names]


def _resolve_operation(client: Any, stack_name: str) -> str:
    if _stack_exists(client, stack_name):
        return "update"
    return "create"


def _start_stack_operation(
    client: Any,
    artifact: CfnArtifact,
    stack_name: str,
    operation: str,
) -> str | None:
    template_yaml = artifact.properties.get("template_yaml")
    if not isinstance(template_yaml, str) or not template_yaml.strip():
        raise RuntimeError(f"Stack {stack_name} is missing template_yaml")

    common_args: dict[str, Any] = {
        "StackName": stack_name,
        "TemplateBody": template_yaml,
        "Capabilities": ["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"],
    }

    try:
        if operation == "create":
            response = client.create_stack(**common_args)
            return response.get("StackId")
        response = client.update_stack(**common_args)
        return response.get("StackId")
    except ClientError as exc:
        message = str(exc)
        if operation == "update" and "No updates are to be performed" in message:
            return None
        raise


def _poll_stack_until_terminal(
    *,
    client: Any,
    stack_name: str,
    max_attempts: int,
    poll_interval_seconds: float,
    sleep_provider: SleepCallable,
) -> tuple[str, dict[str, str], dict[str, str]]:
    for attempt in range(max_attempts):
        stack = _describe_stack(client, stack_name)
        status = str(stack.get("StackStatus", "UNKNOWN"))
        latest_resource_statuses = _latest_resource_statuses(client, stack_name)

        outputs: dict[str, str] = {}
        raw_outputs = stack.get("Outputs", [])
        if isinstance(raw_outputs, list):
            for item in raw_outputs:
                if not isinstance(item, dict):
                    continue
                key = item.get("OutputKey")
                value = item.get("OutputValue")
                if isinstance(key, str) and isinstance(value, str):
                    outputs[key] = value

        if status in _SUCCESS_STACK_STATUSES or status in _FAILURE_STACK_STATUSES:
            return status, latest_resource_statuses, outputs

        if attempt + 1 < max_attempts and poll_interval_seconds > 0:
            sleep_provider(poll_interval_seconds)

    raise RuntimeError(
        f"Timed out waiting for stack {stack_name} after {max_attempts} polling attempts"
    )


def _rollback_stacks(
    *,
    client: Any,
    failed_stack_name: str,
    successful_stack_names: list[str],
    max_attempts: int,
    poll_interval_seconds: float,
    sleep_provider: SleepCallable,
) -> list[str]:
    """Execute rollback procedure: cancel failed updates, then delete affected stacks in reverse order."""

    actions: list[str] = []
    targets: list[str] = [failed_stack_name, *reversed(successful_stack_names)]
    seen: set[str] = set()

    for stack_name in targets:
        if stack_name in seen:
            continue
        seen.add(stack_name)

        if not _stack_exists(client, stack_name):
            actions.append(f"skip:{stack_name}:missing")
            continue

        status = _describe_stack(client, stack_name).get("StackStatus", "UNKNOWN")
        if isinstance(status, str) and status in _UPDATE_IN_PROGRESS_STATUSES:
            try:
                client.cancel_update_stack(StackName=stack_name)
                actions.append(f"cancel_update:{stack_name}")
            except ClientError as exc:
                actions.append(f"cancel_update_failed:{stack_name}:{exc}")

        try:
            client.delete_stack(StackName=stack_name)
            actions.append(f"delete:{stack_name}")
            _wait_for_stack_deleted(
                client=client,
                stack_name=stack_name,
                max_attempts=max_attempts,
                poll_interval_seconds=poll_interval_seconds,
                sleep_provider=sleep_provider,
            )
            actions.append(f"delete_complete:{stack_name}")
        except ClientError as exc:
            actions.append(f"delete_failed:{stack_name}:{exc}")

    return actions


def _wait_for_stack_deleted(
    *,
    client: Any,
    stack_name: str,
    max_attempts: int,
    poll_interval_seconds: float,
    sleep_provider: SleepCallable,
) -> None:
    for attempt in range(max_attempts):
        if not _stack_exists(client, stack_name):
            return

        status = _describe_stack(client, stack_name).get("StackStatus", "UNKNOWN")
        if status == "DELETE_COMPLETE":
            return
        if status == "DELETE_FAILED":
            raise RuntimeError(f"Stack {stack_name} entered DELETE_FAILED during rollback")

        if attempt + 1 < max_attempts and poll_interval_seconds > 0:
            sleep_provider(poll_interval_seconds)


def _stack_exists(client: Any, stack_name: str) -> bool:
    try:
        client.describe_stacks(StackName=stack_name)
        return True
    except ClientError as exc:
        message = str(exc)
        if "does not exist" in message or "ValidationError" in message:
            return False
        raise


def _describe_stack(client: Any, stack_name: str) -> dict[str, Any]:
    response = client.describe_stacks(StackName=stack_name)
    stacks = response.get("Stacks", [])
    if not isinstance(stacks, list) or not stacks:
        raise RuntimeError(f"Unable to describe stack {stack_name}")
    first = stacks[0]
    if not isinstance(first, dict):
        raise RuntimeError(f"Unexpected CloudFormation response for stack {stack_name}")
    return first


def _latest_resource_statuses(client: Any, stack_name: str) -> dict[str, str]:
    response = client.describe_stack_events(StackName=stack_name)
    events = response.get("StackEvents", [])
    if not isinstance(events, list):
        return {}

    latest: dict[str, str] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        logical_id = event.get("LogicalResourceId")
        status = event.get("ResourceStatus")

        if not isinstance(logical_id, str) or not isinstance(status, str):
            continue
        if logical_id not in latest:
            latest[logical_id] = status

    return latest


def _build_audit_record(
    *,
    state: MigrationState,
    request: DeploymentRequest,
    result: DeploymentResult,
    attempted_stacks: list[str],
    deployed_stacks: list[str],
    timestamp: datetime,
) -> AuditRecord:
    approval = state.approval_record
    approval_reference = None
    reviewer = None
    if approval is not None:
        reviewer = approval.reviewer
        approval_reference = {
            "plan_hash": approval.plan_hash,
            "decision": approval.decision.value,
            "reviewer": approval.reviewer,
            "timestamp": approval.timestamp.isoformat(),
        }

    return AuditRecord(
        inputs_hash=_stable_hash(
            {
                "run_id": state.run_id,
                "plan_hash": state.plan_hash,
                "request": request.model_dump(mode="json"),
                "artifacts": [artifact.model_dump(mode="json") for artifact in state.cfn_artifacts],
            }
        ),
        outputs={
            "stage": "deployer",
            "deployed": result.deployed,
            "attempted_stacks": attempted_stacks,
            "deployed_stacks": deployed_stacks,
            "failed_stack": result.failed_stack,
            "rollback_triggered": result.rollback_triggered,
            "approval_reference": approval_reference,
        },
        rule_ids_used=sorted({rule for artifact in state.cfn_artifacts for rule in artifact.rule_ids_used}),
        reviewer=reviewer,
        timestamp=timestamp,
    )


def _stable_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
