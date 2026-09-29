"""Static validation agent node for generated CloudFormation artifacts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.agents.generator import GeneratorRequest, generator_node
from src.graph.state import AuditRecord, FindingSeverity, MigrationState, ValidationFinding


class ValidatorRequest(BaseModel):
    """Input contract for static validation stage execution."""

    model_config = ConfigDict(extra="forbid")

    config_path: str | None = None
    max_retries: int = Field(default=2, ge=0, le=5)


class _CommandResult(BaseModel):
    """Normalized subprocess output payload."""

    model_config = ConfigDict(extra="forbid")

    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[list[str], Path], _CommandResult]
GeneratorCallable = Callable[[MigrationState, GeneratorRequest], MigrationState]
NowCallable = Callable[[], datetime]


def validator_node(
    state: MigrationState,
    request: ValidatorRequest,
    *,
    command_runner: CommandRunner | None = None,
    generator: GeneratorCallable | None = None,
    now_provider: NowCallable | None = None,
) -> MigrationState:
    """Run cfn-lint and checkov, retry generation on blocking findings, and fail on unresolved lint/security blockers."""

    if not state.cfn_artifacts:
        timestamp = _now(now_provider)
        audit = _build_audit(
            state=state,
            request=request,
            findings=[],
            retries_used=0,
            timestamp=timestamp,
        )
        return state.model_copy(
            update={
                "updated_at": timestamp,
                "validation_findings": [],
                "audit_records": [*state.audit_records, audit],
            }
        )

    run_command = command_runner or _run_command
    regenerate = generator or generator_node

    working_state = state
    findings: list[ValidationFinding] = []

    for attempt in range(request.max_retries + 1):
        findings = _collect_findings(working_state, run_command)
        lint_failures = [
            finding
            for finding in findings
            if finding.stage == "cfn-lint"
            and finding.severity in {FindingSeverity.HIGH, FindingSeverity.CRITICAL}
        ]
        security_blockers = [
            finding
            for finding in findings
            if _is_least_privilege_violation(finding) or _is_public_access_violation(finding)
        ]

        has_blockers = bool(lint_failures or security_blockers)
        can_retry = attempt < request.max_retries

        if has_blockers and can_retry:
            working_state = regenerate(
                working_state,
                GeneratorRequest(
                    config_path=request.config_path,
                    validation_feedback=findings,
                    retry_attempt=attempt + 1,
                ),
            )
            continue

        timestamp = _now(now_provider)
        audit = _build_audit(
            state=working_state,
            request=request,
            findings=findings,
            retries_used=attempt,
            timestamp=timestamp,
        )

        if lint_failures:
            raise RuntimeError(
                "Static validation failed: cfn-lint findings remain after retries "
                f"({len(lint_failures)} finding(s))."
            )

        if security_blockers:
            raise RuntimeError(
                "Static validation failed: unresolved least-privilege/public-access blockers "
                f"after retries ({len(security_blockers)} finding(s))."
            )

        return working_state.model_copy(
            update={
                "updated_at": timestamp,
                "validation_findings": findings,
                "audit_records": [*working_state.audit_records, audit],
            }
        )

    raise RuntimeError("Static validation failed unexpectedly before producing a terminal outcome.")


def _collect_findings(state: MigrationState, run_command: CommandRunner) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []

    with tempfile.TemporaryDirectory(prefix="validator-") as temp_dir:
        workdir = Path(temp_dir)

        for artifact in state.cfn_artifacts:
            template_yaml = artifact.properties.get("template_yaml")
            if not isinstance(template_yaml, str):
                continue

            template_path = workdir / f"{artifact.logical_id}.yaml"
            template_path.write_text(template_yaml, encoding="utf-8")

            lint_result = run_command(
                ["cfn-lint", "--format", "json", str(template_path)],
                workdir,
            )
            lint_payload = _extract_json_payload(lint_result.stdout)
            findings.extend(_parse_cfn_lint_findings(lint_payload, artifact.logical_id))
            if lint_result.returncode not in (0, 2, 4):
                findings.append(
                    ValidationFinding(
                        check_id="CFN_LINT_EXECUTION",
                        message=(
                            "cfn-lint failed to execute cleanly: "
                            f"{lint_result.stderr.strip() or 'unknown error'}"
                        ),
                        severity=FindingSeverity.HIGH,
                        resource_logical_id=artifact.logical_id,
                        stage="cfn-lint",
                    )
                )

            checkov_result = run_command(
                [
                    "checkov",
                    "-f",
                    str(template_path),
                    "--framework",
                    "cloudformation",
                    "-o",
                    "json",
                    "--quiet",
                ],
                workdir,
            )
            checkov_payload = _extract_json_payload(checkov_result.stdout)
            findings.extend(_parse_checkov_findings(checkov_payload, artifact.logical_id))
            if checkov_result.returncode not in (0, 1):
                findings.append(
                    ValidationFinding(
                        check_id="CHECKOV_EXECUTION",
                        message=(
                            "checkov failed to execute cleanly: "
                            f"{checkov_result.stderr.strip() or 'unknown error'}"
                        ),
                        severity=FindingSeverity.HIGH,
                        resource_logical_id=artifact.logical_id,
                        stage="checkov",
                    )
                )

    return findings


def _run_command(command: list[str], cwd: Path) -> _CommandResult:
    if not command:
        raise RuntimeError("Validator command must not be empty")

    executable = _resolve_executable(command[0])
    resolved_command = [executable, *command[1:]]

    try:
        completed = subprocess.run(
            resolved_command,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        tool_name = command[0]
        raise RuntimeError(f"Required tool not found on PATH: {tool_name}") from exc

    return _CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _resolve_executable(tool_name: str) -> str:
    resolved = shutil.which(tool_name)
    if resolved is not None:
        return resolved

    scripts_dir = Path(sys.executable).resolve().parent
    candidates = [
        scripts_dir / tool_name,
        scripts_dir / f"{tool_name}.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return tool_name


def _extract_json_payload(raw_output: str) -> Any:
    text = raw_output.strip()
    if not text:
        return []

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", text, flags=re.DOTALL)
        if not match:
            return []
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return []


def _parse_cfn_lint_findings(payload: Any, fallback_resource: str) -> list[ValidationFinding]:
    entries: list[dict[str, Any]] = []

    if isinstance(payload, list):
        entries = [entry for entry in payload if isinstance(entry, dict)]
    elif isinstance(payload, dict):
        if isinstance(payload.get("Matches"), list):
            entries = [entry for entry in payload["Matches"] if isinstance(entry, dict)]
        else:
            entries = [payload]

    findings: list[ValidationFinding] = []
    for entry in entries:
        rule = entry.get("Rule")
        rule_id = "UNKNOWN"
        if isinstance(rule, dict):
            rule_id = str(rule.get("Id", "UNKNOWN"))
        elif "RuleId" in entry:
            rule_id = str(entry.get("RuleId"))

        level_raw = str(entry.get("Level", entry.get("level", "ERROR"))).upper()
        if level_raw == "WARNING":
            severity = FindingSeverity.MEDIUM
        elif level_raw in {"INFO", "INFORMATION", "INFORMATIONAL"}:
            severity = FindingSeverity.LOW
        else:
            severity = FindingSeverity.HIGH

        resource_logical_id = _coerce_optional_str(
            entry.get("LogicalResourceId")
            or entry.get("Resource")
            or entry.get("resource")
            or fallback_resource
        )

        findings.append(
            ValidationFinding(
                check_id=f"CFN_LINT_{rule_id}",
                message=str(entry.get("Message", entry.get("message", "cfn-lint finding"))),
                severity=severity,
                resource_logical_id=resource_logical_id,
                stage="cfn-lint",
            )
        )

    return findings


def _parse_checkov_findings(payload: Any, fallback_resource: str) -> list[ValidationFinding]:
    failed_checks: list[dict[str, Any]] = []

    if isinstance(payload, dict):
        failed_checks.extend(_extract_failed_checks(payload))
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                failed_checks.extend(_extract_failed_checks(item))

    findings: list[ValidationFinding] = []
    for check in failed_checks:
        check_id = str(check.get("check_id", "CHECKOV_UNKNOWN"))
        message = str(check.get("check_name") or check.get("guideline") or "checkov finding")

        severity = _map_checkov_severity(check.get("severity"))
        resource_logical_id = _coerce_optional_str(check.get("resource") or fallback_resource)

        findings.append(
            ValidationFinding(
                check_id=check_id,
                message=message,
                severity=severity,
                resource_logical_id=resource_logical_id,
                stage="checkov",
            )
        )

    return findings


def _extract_failed_checks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("results"), dict):
        failed = payload["results"].get("failed_checks")
        if isinstance(failed, list):
            return [item for item in failed if isinstance(item, dict)]

    failed_checks = payload.get("failed_checks")
    if isinstance(failed_checks, list):
        return [item for item in failed_checks if isinstance(item, dict)]

    return []


def _map_checkov_severity(raw: Any) -> FindingSeverity:
    if isinstance(raw, dict):
        raw_value = str(raw.get("level", "MEDIUM"))
    else:
        raw_value = str(raw or "MEDIUM")

    normalized = raw_value.upper()
    if normalized == "CRITICAL":
        return FindingSeverity.CRITICAL
    if normalized == "HIGH":
        return FindingSeverity.HIGH
    if normalized == "LOW":
        return FindingSeverity.LOW
    return FindingSeverity.MEDIUM


def _is_least_privilege_violation(finding: ValidationFinding) -> bool:
    haystack = f"{finding.check_id} {finding.message}".lower()
    patterns = (
        "least privilege",
        "least-privilege",
        "wildcard action",
        "wildcard resource",
        "action '*'",
        "resource '*'",
        "full access",
        "administratoraccess",
        "overly permissive",
    )
    return any(pattern in haystack for pattern in patterns)


def _is_public_access_violation(finding: ValidationFinding) -> bool:
    haystack = f"{finding.check_id} {finding.message}".lower()
    patterns = (
        "public",
        "internet",
        "0.0.0.0/0",
        "anonymous",
        "all users",
        "world-readable",
        "world writable",
        "principal '*'",
    )
    return any(pattern in haystack for pattern in patterns)


def _build_audit(
    *,
    state: MigrationState,
    request: ValidatorRequest,
    findings: list[ValidationFinding],
    retries_used: int,
    timestamp: datetime,
) -> AuditRecord:
    input_hash = _stable_hash(
        {
            "run_id": state.run_id,
            "request": request.model_dump(mode="json"),
            "artifacts": [
                {
                    "logical_id": artifact.logical_id,
                    "resource_type": artifact.resource_type,
                    "template_hash": _stable_hash(
                        {
                            "template_yaml": str(artifact.properties.get("template_yaml", "")),
                        }
                    ),
                }
                for artifact in state.cfn_artifacts
            ],
        }
    )

    return AuditRecord(
        inputs_hash=input_hash,
        outputs={
            "stage": "validator",
            "artifact_count": len(state.cfn_artifacts),
            "finding_count": len(findings),
            "cfn_lint_finding_count": sum(1 for finding in findings if finding.stage == "cfn-lint"),
            "checkov_finding_count": sum(1 for finding in findings if finding.stage == "checkov"),
            "security_blocker_count": sum(
                1
                for finding in findings
                if _is_least_privilege_violation(finding) or _is_public_access_violation(finding)
            ),
            "retries_used": retries_used,
        },
        rule_ids_used=sorted({rule_id for artifact in state.cfn_artifacts for rule_id in artifact.rule_ids_used}),
        reviewer=None,
        timestamp=timestamp,
    )


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _now(now_provider: NowCallable | None) -> datetime:
    return now_provider() if now_provider is not None else datetime.now(tz=timezone.utc)
