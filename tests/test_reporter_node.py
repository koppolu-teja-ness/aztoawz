from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.agents.reporter import ReporterRequest, reporter_node
from src.graph.state import (
    ApprovalDecision,
    ApprovalRecord,
    CfnArtifact,
    DeploymentResult,
    DiscoveredResource,
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


def _state_fixture() -> MigrationState:
    now = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)
    return MigrationState(
        run_id="run-reporter-001",
        started_at=now,
        updated_at=now,
        discovered_resources=[
            DiscoveredResource(
                resource_id="Microsoft.KeyVault/vaults/kv-app",
                resource_type="Microsoft.KeyVault/vaults",
                name="kv-app",
                resource_group="rg-migration",
                region="eastus",
                tags={"owner": "security-team"},
                raw_properties={"enableRbacAuthorization": True},
            ),
            DiscoveredResource(
                resource_id="Microsoft.Web/sites/func-app",
                resource_type="Microsoft.Web/sites",
                name="func-app",
                resource_group="rg-migration",
                region="eastus",
                tags={"owner": "app-team"},
                raw_properties={"httpsOnly": True},
            ),
        ],
        parsed_resources=[
            ParsedResource(
                resource_id="Microsoft.KeyVault/vaults/kv-app",
                source_type="Microsoft.KeyVault/vaults",
                normalized_type="microsoft_keyvault_vaults",
                properties={"softDelete": True},
                dependencies=[],
            ),
            ParsedResource(
                resource_id="Microsoft.Web/sites/func-app",
                source_type="Microsoft.Web/sites",
                normalized_type="microsoft_web_sites",
                properties={"httpsOnly": True},
                dependencies=["Microsoft.KeyVault/vaults/kv-app"],
            ),
            ParsedResource(
                resource_id="Contoso.Custom/resources/custom-1",
                source_type="Contoso.Custom/resources",
                normalized_type="contoso_custom_resources",
                properties={"mode": "unsupported"},
                dependencies=[],
            ),
        ],
        mapping_results=[
            MappingResult(
                source_resource_id="Microsoft.KeyVault/vaults/kv-app",
                target_resource_type="AWS::SecretsManager::Secret + AWS::KMS::Key",
                mapped=True,
                rule_id="kv-001-vault",
                confidence=0.97,
                caveats=[],
            ),
            MappingResult(
                source_resource_id="Microsoft.Web/sites/func-app",
                target_resource_type="AWS::Lambda::Function",
                mapped=True,
                rule_id="fn-001-function-app",
                confidence=0.94,
                caveats=["Confirm runtime parity"],
            ),
            MappingResult(
                source_resource_id="Contoso.Custom/resources/custom-1",
                target_resource_type="UNMAPPED",
                mapped=False,
                rule_id="UNMAPPED",
                confidence=0.0,
                caveats=["requires manual intervention"],
            ),
        ],
        cfn_artifacts=[
            CfnArtifact(
                logical_id="KeyVaultStack",
                resource_type="AWS::CloudFormation::Stack",
                properties={"unit": "keyvault", "template_yaml": "Resources: {}"},
                rule_ids_used=["kv-001-vault"],
            ),
            CfnArtifact(
                logical_id="FunctionsStack",
                resource_type="AWS::CloudFormation::Stack",
                properties={"unit": "functions", "template_yaml": "Resources: {}"},
                rule_ids_used=["fn-001-function-app"],
            ),
        ],
        validation_findings=[
            ValidationFinding(
                check_id="CFN_LINT_E3001",
                message="Template structure valid",
                severity=FindingSeverity.LOW,
                resource_logical_id="FunctionsStack",
                stage="cfn-lint",
            ),
            ValidationFinding(
                check_id="CKV_AWS_111",
                message="IAM policy allows wildcard action",
                severity=FindingSeverity.HIGH,
                resource_logical_id="FunctionsStack",
                stage="checkov",
            ),
        ],
        risk_scores=[
            RiskScore(
                resource_id="Microsoft.KeyVault/vaults/kv-app",
                score=20,
                rationale=["High-confidence mapping with scoped controls"],
            ),
            RiskScore(
                resource_id="Microsoft.Web/sites/func-app",
                score=48,
                rationale=["Runtime compatibility needs validation"],
            ),
            RiskScore(
                resource_id="Contoso.Custom/resources/custom-1",
                score=100,
                rationale=["Unsupported source resource type"],
            ),
        ],
        migration_plan=[
            MigrationPlanItem(
                resource_id="Microsoft.KeyVault/vaults/kv-app",
                action="Deploy mapped secret and key resources",
                status=PlanItemStatus.AUTO,
                risk_score=20,
                notes=["No blockers"],
            ),
            MigrationPlanItem(
                resource_id="Microsoft.Web/sites/func-app",
                action="Deploy Lambda and validate runtime behavior",
                status=PlanItemStatus.REVIEW,
                risk_score=48,
                notes=["Review runtime and networking controls"],
            ),
            MigrationPlanItem(
                resource_id="Contoso.Custom/resources/custom-1",
                action="Manual migration required",
                status=PlanItemStatus.HIGH_RISK,
                risk_score=100,
                notes=["UNMAPPED by KB"],
            ),
        ],
        plan_hash="sha256:reporter-plan-hash",
        approval_record=ApprovalRecord(
            decision=ApprovalDecision.APPROVE,
            reviewer="approver@example.com",
            timestamp=datetime(2026, 9, 29, 18, 5, tzinfo=timezone.utc),
            plan_hash="sha256:reporter-plan-hash",
            comments="Approved after security review",
        ),
        deployment_result=DeploymentResult(
            deployed=True,
            stack_name="migration-main",
            stack_id="stack-001",
            outputs={"FunctionName": "func-app"},
            stack_statuses={"KeyVaultStack": "CREATE_COMPLETE", "FunctionsStack": "CREATE_COMPLETE"},
            resource_statuses={"FunctionsStack": {"FuncRole": "CREATE_COMPLETE"}},
            rollback_triggered=False,
            rollback_actions=[],
            failed_stack=None,
            message="Deployment succeeded",
            timestamp=datetime(2026, 9, 29, 18, 8, tzinfo=timezone.utc),
        ),
        post_deploy_findings=[
            PostDeployFinding(
                check_id="SECURITY_POSTURE_SUMMARY",
                passed=True,
                message="No broadened IAM scope detected",
                severity=FindingSeverity.LOW,
            )
        ],
        manual_dependencies=[
            "UNMAPPED requires manual intervention: Contoso.Custom/resources/custom-1"
        ],
    )


def _assert_section_present_and_non_empty(markdown: str, heading: str) -> None:
    lines = markdown.splitlines()
    target = f"## {heading}"
    assert target in lines

    start_index = lines.index(target) + 1
    end_index = len(lines)
    for index in range(start_index, len(lines)):
        if lines[index].startswith("## "):
            end_index = index
            break

    section_lines = [line.strip() for line in lines[start_index:end_index] if line.strip()]
    assert section_lines, f"Section '{heading}' should not be empty"


def test_reporter_node_snapshot_contains_all_required_sections(tmp_path: Path) -> None:
    state = _state_fixture()
    output_dir = tmp_path / "reports"

    updated = reporter_node(
        state,
        ReporterRequest(output_dir=str(output_dir)),
        now_provider=lambda: datetime(2026, 9, 29, 18, 10, tzinfo=timezone.utc),
    )

    report_dir = output_dir / state.run_id
    migration_plan_path = report_dir / "migration-plan.md"
    risk_report_path = report_dir / "risk-report.md"
    execution_report_path = report_dir / "execution-report.md"
    validation_report_path = report_dir / "validation-report.md"

    for path in [migration_plan_path, risk_report_path, execution_report_path, validation_report_path]:
        assert path.exists()

    migration_plan_markdown = migration_plan_path.read_text(encoding="utf-8")
    risk_report_markdown = risk_report_path.read_text(encoding="utf-8")
    execution_report_markdown = execution_report_path.read_text(encoding="utf-8")
    validation_report_markdown = validation_report_path.read_text(encoding="utf-8")

    _assert_section_present_and_non_empty(migration_plan_markdown, "Plan Table")
    _assert_section_present_and_non_empty(migration_plan_markdown, "Mapping Decisions And Rule IDs")
    _assert_section_present_and_non_empty(migration_plan_markdown, "Manual-Handling Dependencies")
    _assert_section_present_and_non_empty(migration_plan_markdown, "Approval Trail")

    _assert_section_present_and_non_empty(risk_report_markdown, "Risk Assessment")
    _assert_section_present_and_non_empty(risk_report_markdown, "UNMAPPED Summary")
    _assert_section_present_and_non_empty(risk_report_markdown, "Rule IDs Referenced")
    _assert_section_present_and_non_empty(risk_report_markdown, "Approval Trail")

    _assert_section_present_and_non_empty(execution_report_markdown, "Execution Timeline")
    _assert_section_present_and_non_empty(execution_report_markdown, "Deployment Outcome")
    _assert_section_present_and_non_empty(execution_report_markdown, "Rule IDs Used During Mapping")
    _assert_section_present_and_non_empty(execution_report_markdown, "Approval Trail")

    _assert_section_present_and_non_empty(validation_report_markdown, "Validation Findings")
    _assert_section_present_and_non_empty(validation_report_markdown, "Rule IDs Referenced By Artifacts")
    _assert_section_present_and_non_empty(validation_report_markdown, "Approval Trail")

    for report_text in [
        migration_plan_markdown,
        risk_report_markdown,
        execution_report_markdown,
        validation_report_markdown,
    ]:
        assert "kv-001-vault" in report_text
        assert "fn-001-function-app" in report_text
        assert "approver@example.com" in report_text
        assert "sha256:reporter-plan-hash" in report_text

    assert updated.audit_records[-1].outputs["stage"] == "reporter"
    generated_reports = updated.audit_records[-1].outputs["generated_reports"]
    assert isinstance(generated_reports, list)
    assert any(str(migration_plan_path.as_posix()) == item for item in generated_reports)
