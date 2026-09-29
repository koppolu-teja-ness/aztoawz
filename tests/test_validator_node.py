from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.agents.generator import GeneratorRequest
from src.agents.validator import ValidatorRequest, validator_node
from src.graph.state import CfnArtifact, MigrationState


class _RunnerResult:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _base_state() -> MigrationState:
    now = datetime(2026, 9, 29, 13, 0, tzinfo=timezone.utc)
    return MigrationState(
        run_id="run-validator-001",
        started_at=now,
        updated_at=now,
        cfn_artifacts=[
            CfnArtifact(
                logical_id="FunctionsStack",
                resource_type="AWS::CloudFormation::Stack",
                properties={
                    "template_yaml": "AWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n"
                },
                rule_ids_used=["FN-001"],
            )
        ],
    )


def test_validator_retries_on_security_finding_and_then_succeeds() -> None:
    checkov_calls = 0
    generator_calls = 0

    def command_runner(command: list[str], _cwd: object) -> _RunnerResult:
        nonlocal checkov_calls
        if command[0] == "cfn-lint":
            return _RunnerResult(returncode=0, stdout="[]")

        checkov_calls += 1
        if checkov_calls == 1:
            return _RunnerResult(
                returncode=1,
                stdout=(
                    '{"results":{"failed_checks":[{"check_id":"CKV_AWS_999",'
                    '"check_name":"Wildcard action in IAM policy Action *",'
                    '"severity":"HIGH","resource":"FunctionExecutionRole"}]}}'
                ),
            )
        return _RunnerResult(returncode=0, stdout='{"results":{"failed_checks":[]}}')

    def regenerate(state: MigrationState, request: GeneratorRequest) -> MigrationState:
        nonlocal generator_calls
        generator_calls += 1
        assert request.retry_attempt == 1
        assert request.validation_feedback
        return state

    updated = validator_node(
        _base_state(),
        ValidatorRequest(max_retries=2),
        command_runner=command_runner,
        generator=regenerate,
        now_provider=lambda: datetime(2026, 9, 29, 13, 1, tzinfo=timezone.utc),
    )

    assert generator_calls == 1
    assert updated.validation_findings == []
    assert updated.audit_records[-1].outputs["retries_used"] == 1


def test_validator_fails_when_cfn_lint_still_fails_after_retries() -> None:
    generator_calls = 0

    def command_runner(command: list[str], _cwd: object) -> _RunnerResult:
        if command[0] == "cfn-lint":
            return _RunnerResult(
                returncode=2,
                stdout=(
                    '[{"Rule":{"Id":"E3001"},"Message":"Invalid template",'
                    '"Level":"Error","LogicalResourceId":"FunctionsStack"}]'
                ),
            )
        return _RunnerResult(returncode=0, stdout='{"results":{"failed_checks":[]}}')

    def regenerate(state: MigrationState, request: GeneratorRequest) -> MigrationState:
        nonlocal generator_calls
        generator_calls += 1
        assert request.retry_attempt == 1
        return state

    with pytest.raises(RuntimeError, match="cfn-lint findings remain"):
        validator_node(
            _base_state(),
            ValidatorRequest(max_retries=1),
            command_runner=command_runner,
            generator=regenerate,
            now_provider=lambda: datetime(2026, 9, 29, 13, 2, tzinfo=timezone.utc),
        )

    assert generator_calls == 1


def test_validator_fails_when_security_blocker_persists_after_retries() -> None:
    def command_runner(command: list[str], _cwd: object) -> _RunnerResult:
        if command[0] == "cfn-lint":
            return _RunnerResult(returncode=0, stdout="[]")
        return _RunnerResult(
            returncode=1,
            stdout=(
                '{"results":{"failed_checks":[{"check_id":"CKV_AWS_998",'
                '"check_name":"Public access allowed via 0.0.0.0/0",'
                '"severity":"HIGH","resource":"FunctionSecurityGroup"}]}}'
            ),
        )

    with pytest.raises(RuntimeError, match="least-privilege/public-access blockers"):
        validator_node(
            _base_state(),
            ValidatorRequest(max_retries=1),
            command_runner=command_runner,
            generator=lambda state, _request: state,
            now_provider=lambda: datetime(2026, 9, 29, 13, 3, tzinfo=timezone.utc),
        )
