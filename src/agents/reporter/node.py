"""Reporting agent node for stakeholder-facing migration reports."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.graph.state import (
    ApprovalDecision,
    AuditRecord,
    FindingSeverity,
    MigrationState,
    PlanItemStatus,
)


class ReporterRequest(BaseModel):
    """Input contract for reporting stage execution."""

    model_config = ConfigDict(extra="forbid")

    output_dir: str = "docs/reports"
    write_html: bool = False
    write_pdf: bool = False


NowCallable = Callable[[], datetime]


def reporter_node(
    state: MigrationState,
    request: ReporterRequest,
    *,
    now_provider: NowCallable | None = None,
) -> MigrationState:
    """Generate plan/risk/execution/validation reports and persist artifacts."""

    now_fn = now_provider or (lambda: datetime.now(tz=timezone.utc))
    timestamp = now_fn()

    output_root = Path(request.output_dir) / state.run_id
    output_root.mkdir(parents=True, exist_ok=True)

    markdown_reports = _build_markdown_reports(state=state, generated_at=timestamp)

    generated_paths: list[str] = []
    export_warnings: list[str] = []

    for report_name, markdown in markdown_reports.items():
        markdown_path = output_root / f"{report_name}.md"
        markdown_path.write_text(markdown, encoding="utf-8")
        generated_paths.append(str(markdown_path.as_posix()))

        if request.write_html:
            html_path = output_root / f"{report_name}.html"
            html_payload = _markdown_to_html(markdown)
            html_path.write_text(html_payload, encoding="utf-8")
            generated_paths.append(str(html_path.as_posix()))

        if request.write_pdf:
            pdf_path = output_root / f"{report_name}.pdf"
            warning = _try_write_pdf(markdown, pdf_path)
            if warning is None:
                generated_paths.append(str(pdf_path.as_posix()))
            else:
                export_warnings.append(warning)

    rule_ids_used = sorted(
        {
            mapping.rule_id
            for mapping in state.mapping_results
            if mapping.rule_id and mapping.rule_id != "UNMAPPED"
        }
        | {rule_id for artifact in state.cfn_artifacts for rule_id in artifact.rule_ids_used if rule_id}
    )

    audit = AuditRecord(
        inputs_hash=_stable_hash(
            {
                "run_id": state.run_id,
                "request": request.model_dump(mode="json"),
                "plan_hash": state.plan_hash,
                "mapping_result_count": len(state.mapping_results),
                "migration_plan_count": len(state.migration_plan),
                "validation_finding_count": len(state.validation_findings),
                "post_deploy_finding_count": len(state.post_deploy_findings),
                "has_approval_record": state.approval_record is not None,
                "has_deployment_result": state.deployment_result is not None,
            }
        ),
        outputs={
            "stage": "reporter",
            "output_root": str(output_root.as_posix()),
            "generated_reports": generated_paths,
            "export_warnings": export_warnings,
        },
        rule_ids_used=rule_ids_used,
        reviewer=state.approval_record.reviewer if state.approval_record else None,
        timestamp=timestamp,
    )

    return state.model_copy(
        update={
            "updated_at": timestamp,
            "audit_records": [*state.audit_records, audit],
        }
    )


def _build_markdown_reports(*, state: MigrationState, generated_at: datetime) -> dict[str, str]:
    return {
        "migration-plan": _render_migration_plan_report(state, generated_at),
        "risk-report": _render_risk_report(state, generated_at),
        "execution-report": _render_execution_report(state, generated_at),
        "validation-report": _render_validation_report(state, generated_at),
    }


def _render_migration_plan_report(state: MigrationState, generated_at: datetime) -> str:
    parsed_by_id = {item.resource_id: item for item in state.parsed_resources}

    lines = [
        "# Migration Plan",
        "",
        f"- Run ID: {state.run_id}",
        f"- Generated At (UTC): {generated_at.isoformat()}",
        f"- Plan Hash: {state.plan_hash or 'UNAVAILABLE'}",
        f"- Total Plan Items: {len(state.migration_plan)}",
        "",
        "## Plan Table",
        "",
        "| Stage | Resource | Sequence | Dependency | Owner |",
        "| --- | --- | --- | --- | --- |",
    ]

    for index, item in enumerate(state.migration_plan, start=1):
        parsed = parsed_by_id.get(item.resource_id)
        stage = _stage_name(parsed.source_type) if parsed else "Unknown"
        dependencies = ", ".join(parsed.dependencies) if parsed and parsed.dependencies else "None"
        owner = _owner_for_resource(item.resource_id, state)
        lines.append(f"| {stage} | {item.resource_id} | {index} | {dependencies} | {owner} |")

    if not state.migration_plan:
        lines.append("| N/A | No plan items generated | - | - | - |")

    lines.extend(
        [
            "",
            "## Mapping Decisions And Rule IDs",
            "",
            "| Source Resource | Mapped | Target Construct | rule_id | Confidence | Caveats |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )

    for mapping in state.mapping_results:
        caveats = "; ".join(mapping.caveats) if mapping.caveats else "None"
        lines.append(
            "| "
            f"{mapping.source_resource_id} | "
            f"{'Yes' if mapping.mapped else 'No'} | "
            f"{mapping.target_resource_type} | "
            f"{mapping.rule_id} | "
            f"{mapping.confidence:.2f} | "
            f"{caveats} |"
        )

    if not state.mapping_results:
        lines.append("| N/A | No | UNMAPPED | UNMAPPED | 0.00 | No mapping decisions available |")

    lines.extend(
        [
            "",
            "## Manual-Handling Dependencies",
            "",
        ]
    )

    if state.manual_dependencies:
        lines.extend([f"- {item}" for item in state.manual_dependencies])
    else:
        lines.append("- None")

    lines.extend(["", *_render_approval_trail_section(state)])
    return "\n".join(lines).strip() + "\n"


def _render_risk_report(state: MigrationState, generated_at: datetime) -> str:
    risk_by_resource = {item.resource_id: item for item in state.risk_scores}

    lines = [
        "# Risk Report",
        "",
        f"- Run ID: {state.run_id}",
        f"- Generated At (UTC): {generated_at.isoformat()}",
        f"- Plan Hash: {state.plan_hash or 'UNAVAILABLE'}",
        "",
        "## Risk Assessment",
        "",
        "| Resource | Risk Level | Rationale | Required Action |",
        "| --- | --- | --- | --- |",
    ]

    for item in state.migration_plan:
        risk = risk_by_resource.get(item.resource_id)
        rationale = "; ".join(risk.rationale) if risk and risk.rationale else "; ".join(item.notes)
        if not rationale:
            rationale = "No explicit rationale captured"
        lines.append(
            "| "
            f"{item.resource_id} | "
            f"{item.status.value} ({item.risk_score}) | "
            f"{rationale} | "
            f"{item.action} |"
        )

    if not state.migration_plan:
        lines.append("| N/A | UNKNOWN | No migration plan available | Manual review required |")

    unmapped_items = [item for item in state.mapping_results if not item.mapped]
    lines.extend(["", "## UNMAPPED Summary", ""])
    if unmapped_items:
        for item in unmapped_items:
            lines.append(f"- {item.source_resource_id}: {item.target_resource_type} ({item.rule_id})")
    else:
        lines.append("- No unmapped items")

    lines.extend(["", "## Rule IDs Referenced", ""])
    rule_ids = sorted(
        {
            mapping.rule_id
            for mapping in state.mapping_results
            if mapping.rule_id and mapping.rule_id != "UNMAPPED"
        }
    )
    if rule_ids:
        lines.extend([f"- {rule_id}" for rule_id in rule_ids])
    else:
        lines.append("- None")

    lines.extend(["", *_render_approval_trail_section(state)])
    return "\n".join(lines).strip() + "\n"


def _render_execution_report(state: MigrationState, generated_at: datetime) -> str:
    deployment = state.deployment_result
    start = state.started_at.isoformat()
    end = state.updated_at.isoformat()

    lines = [
        "# Execution Report",
        "",
        f"- Run ID: {state.run_id}",
        f"- Generated At (UTC): {generated_at.isoformat()}",
        f"- Plan Hash: {state.plan_hash or 'UNAVAILABLE'}",
        "",
        "## Execution Timeline",
        "",
        "| Resource | Action | Status | Start/End | Notes |",
        "| --- | --- | --- | --- | --- |",
    ]

    for item in state.migration_plan:
        status = _execution_status(item.status, deployment)
        lines.append(
            "| "
            f"{item.resource_id} | "
            f"{item.action} | "
            f"{status} | "
            f"{start} -> {end} | "
            f"{'; '.join(item.notes) if item.notes else 'None'} |"
        )

    if not state.migration_plan:
        lines.append("| N/A | No execution plan | PENDING | N/A | No plan items available |")

    lines.extend(["", "## Deployment Outcome", ""])
    if deployment is None:
        lines.append("- Deployment has not executed.")
    else:
        lines.append(f"- Deployed: {deployment.deployed}")
        lines.append(f"- Message: {deployment.message or 'N/A'}")
        lines.append(f"- Failed Stack: {deployment.failed_stack or 'None'}")
        lines.append(f"- Rollback Triggered: {deployment.rollback_triggered}")
        if deployment.stack_statuses:
            lines.append("- Stack Statuses:")
            for stack_name, stack_status in sorted(deployment.stack_statuses.items()):
                lines.append(f"  - {stack_name}: {stack_status}")

    lines.extend(["", "## Rule IDs Used During Mapping", ""])
    rule_rows = [
        (item.source_resource_id, item.rule_id)
        for item in state.mapping_results
        if item.rule_id and item.rule_id != "UNMAPPED"
    ]
    if rule_rows:
        for source_resource_id, rule_id in sorted(rule_rows):
            lines.append(f"- {source_resource_id}: {rule_id}")
    else:
        lines.append("- None")

    lines.extend(["", *_render_approval_trail_section(state)])
    return "\n".join(lines).strip() + "\n"


def _render_validation_report(state: MigrationState, generated_at: datetime) -> str:
    reviewer = state.approval_record.reviewer if state.approval_record else "N/A"

    lines = [
        "# Validation Report",
        "",
        f"- Run ID: {state.run_id}",
        f"- Generated At (UTC): {generated_at.isoformat()}",
        f"- Plan Hash: {state.plan_hash or 'UNAVAILABLE'}",
        "",
        "## Validation Findings",
        "",
        "| Check Type | Result | Evidence | Security Delta | Reviewer |",
        "| --- | --- | --- | --- | --- |",
    ]

    for finding in state.validation_findings:
        result = "FAIL" if finding.severity in {FindingSeverity.HIGH, FindingSeverity.CRITICAL} else "PASS"
        evidence = finding.message.replace("|", "\\|")
        security_delta = _validation_security_delta(finding)
        lines.append(
            "| "
            f"{finding.check_id} ({finding.stage or 'validation'}) | "
            f"{result} | "
            f"{evidence} | "
            f"{security_delta} | "
            f"{reviewer} |"
        )

    for finding in state.post_deploy_findings:
        result = "PASS" if finding.passed else "FAIL"
        security_delta = "No regression" if finding.passed else "Potential regression"
        lines.append(
            "| "
            f"{finding.check_id} (post-deploy) | "
            f"{result} | "
            f"{finding.message.replace('|', '\\|')} | "
            f"{security_delta} | "
            f"{reviewer} |"
        )

    if not state.validation_findings and not state.post_deploy_findings:
        lines.append("| N/A | PASS | No findings were recorded | No regression detected | N/A |")

    lines.extend(["", "## Rule IDs Referenced By Artifacts", ""])
    artifact_rule_ids = sorted(
        {
            rule_id
            for artifact in state.cfn_artifacts
            for rule_id in artifact.rule_ids_used
            if rule_id
        }
    )
    if artifact_rule_ids:
        lines.extend([f"- {rule_id}" for rule_id in artifact_rule_ids])
    else:
        lines.append("- None")

    lines.extend(["", *_render_approval_trail_section(state)])
    return "\n".join(lines).strip() + "\n"


def _stage_name(source_type: str) -> str:
    lowered = source_type.lower()
    if lowered.startswith("microsoft.network/"):
        return "Network"
    if lowered.startswith("microsoft.keyvault/"):
        return "KeyVault"
    if lowered.startswith("microsoft.web/sites"):
        return "Functions"
    return "Manual"


def _owner_for_resource(resource_id: str, state: MigrationState) -> str:
    for resource in state.discovered_resources:
        if resource.resource_id != resource_id:
            continue
        owner = resource.tags.get("owner")
        if owner:
            return owner
    return "platform-team"


def _execution_status(status: PlanItemStatus, deployment_result: Any) -> str:
    if deployment_result is None:
        return "PENDING"
    if status == PlanItemStatus.HIGH_RISK and not deployment_result.deployed:
        return "BLOCKED"
    return "SUCCEEDED" if deployment_result.deployed else "FAILED"


def _validation_security_delta(finding: Any) -> str:
    message = f"{finding.check_id} {finding.message}".lower()
    if "wildcard" in message or "public" in message or "0.0.0.0/0" in message:
        return "Broader than source intent"
    if finding.severity in {FindingSeverity.HIGH, FindingSeverity.CRITICAL}:
        return "Needs remediation"
    return "No regression"


def _render_approval_trail_section(state: MigrationState) -> list[str]:
    lines = [
        "## Approval Trail",
        "",
        "| Decision | Reviewer | Timestamp | Plan Hash | Comments |",
        "| --- | --- | --- | --- | --- |",
    ]

    if state.approval_record is not None:
        record = state.approval_record
        lines.append(
            "| "
            f"{record.decision.value} | "
            f"{record.reviewer} | "
            f"{record.timestamp.isoformat()} | "
            f"{record.plan_hash} | "
            f"{(record.comments or '').replace('|', '\\|') or 'None'} |"
        )

    reviewer_events = [audit for audit in state.audit_records if audit.reviewer]
    for event in reviewer_events:
        stage = str(event.outputs.get("stage", "unknown"))
        decision = stage.upper()
        comments = str(event.outputs.get("comments", "Audit reviewer event"))
        plan_hash = str(event.outputs.get("plan_hash", state.plan_hash or "UNKNOWN"))
        lines.append(
            "| "
            f"{decision} | "
            f"{event.reviewer or 'unknown'} | "
            f"{event.timestamp.isoformat()} | "
            f"{plan_hash} | "
            f"{comments.replace('|', '\\|')} |"
        )

    if state.approval_record is None and not reviewer_events:
        lines.append("| NONE | N/A | N/A | N/A | No approval record captured |")

    return lines


def _markdown_to_html(markdown_text: str) -> str:
    try:
        import markdown as markdown_lib

        body = markdown_lib.markdown(markdown_text, extensions=["tables", "fenced_code"])
    except Exception:
        body = f"<pre>{html.escape(markdown_text)}</pre>"

    return "\n".join(
        [
            "<!doctype html>",
            "<html lang=\"en\">",
            "<head>",
            "<meta charset=\"utf-8\" />",
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />",
            "<title>Migration Report</title>",
            "</head>",
            "<body>",
            body,
            "</body>",
            "</html>",
        ]
    )


def _try_write_pdf(markdown_text: str, output_path: Path) -> str | None:
    try:
        from weasyprint import HTML
    except Exception:
        return (
            "PDF export skipped because optional dependency 'weasyprint' is not installed "
            f"for {output_path.name}"
        )

    html_payload = _markdown_to_html(markdown_text)
    try:
        HTML(string=html_payload).write_pdf(str(output_path))
    except Exception as exc:
        return f"PDF export failed for {output_path.name}: {exc}"
    return None


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
