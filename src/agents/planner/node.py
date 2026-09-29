"""Planning and risk-scoring agent node for deployment sequencing."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from src.graph.state import (
    AuditRecord,
    CfnArtifact,
    MappingResult,
    MigrationPlanItem,
    MigrationPlanReport,
    MigrationState,
    ParsedResource,
    PlanItemStatus,
    ResourceDependencyGraph,
    RiskScore,
)
from src.tools.config import load_migration_config


class PlannerRequest(BaseModel):
    """Input contract for planner stage execution."""

    model_config = ConfigDict(extra="forbid")

    config_path: str | None = None


NowCallable = Callable[[], datetime]


def planner_node(
    state: MigrationState,
    request: PlannerRequest,
    *,
    now_provider: NowCallable | None = None,
) -> MigrationState:
    """Build deployment-ordered plan items, risk labels, and deterministic plan hash."""

    now_fn = now_provider or (lambda: datetime.now(tz=timezone.utc))
    config = load_migration_config(request.config_path)

    parsed_by_id = {resource.resource_id: resource for resource in state.parsed_resources}
    mapping_by_resource = {result.source_resource_id: result for result in state.mapping_results}
    artifact_flags_by_unit = _artifact_flags_by_unit(state.cfn_artifacts)
    ordered_resource_ids = _ordered_resource_ids(state.parsed_resources, state.parsed_dependency_graph)

    plan_items: list[MigrationPlanItem] = []
    risk_scores: list[RiskScore] = []

    for resource_id in ordered_resource_ids:
        resource = parsed_by_id[resource_id]
        mapping_result = mapping_by_resource.get(resource_id)
        plan_item, risk_score = _build_plan_item(
            resource=resource,
            mapping_result=mapping_result,
            artifact_flags_by_unit=artifact_flags_by_unit,
            manual_dependencies=state.manual_dependencies,
            thresholds={
                "auto_max": config.risk_thresholds.auto_migratable_max_score,
                "review_max": config.risk_thresholds.requires_review_max_score,
                "high_min": config.risk_thresholds.high_risk_min_score,
            },
            security_flags=config.security,
        )
        plan_items.append(plan_item)
        risk_scores.append(risk_score)

    plan_hash = _compute_plan_hash(plan_items, state.cfn_artifacts)

    report = _build_plan_report(
        plan_items=plan_items,
        parsed_by_id=parsed_by_id,
        owner=config.tagging_standard.get("owner", "platform-team"),
        plan_hash=plan_hash,
    )

    timestamp = now_fn()
    audit = AuditRecord(
        inputs_hash=_stable_hash(
            {
                "run_id": state.run_id,
                "request": request.model_dump(mode="json"),
                "parsed_resources": [item.model_dump(mode="json") for item in state.parsed_resources],
                "dependency_graph": (
                    state.parsed_dependency_graph.model_dump(mode="json")
                    if state.parsed_dependency_graph
                    else None
                ),
                "mapping_results": [item.model_dump(mode="json") for item in state.mapping_results],
                "manual_dependencies": state.manual_dependencies,
                "artifact_logical_ids": [artifact.logical_id for artifact in state.cfn_artifacts],
            }
        ),
        outputs={
            "stage": "planner",
            "plan_hash": plan_hash,
            "plan_item_count": len(plan_items),
            "auto_migratable_count": report.auto_migratable_count,
            "requires_review_count": report.requires_review_count,
            "high_risk_count": report.high_risk_count,
        },
        rule_ids_used=sorted(
            {
                mapping.rule_id
                for mapping in state.mapping_results
                if mapping.rule_id and mapping.rule_id != "UNMAPPED"
            }
        ),
        reviewer=None,
        timestamp=timestamp,
    )

    return state.model_copy(
        update={
            "updated_at": timestamp,
            "risk_scores": risk_scores,
            "migration_plan": plan_items,
            "plan_hash": plan_hash,
            "migration_plan_report": report,
            "audit_records": [*state.audit_records, audit],
        }
    )


def _ordered_resource_ids(
    resources: list[ParsedResource],
    dependency_graph: ResourceDependencyGraph | None,
) -> list[str]:
    resource_ids = [resource.resource_id for resource in resources]
    known_ids = set(resource_ids)
    dependencies: dict[str, set[str]] = {resource_id: set() for resource_id in resource_ids}

    if dependency_graph is not None:
        for edge in dependency_graph.edges:
            if edge.to_resource_id in known_ids and edge.from_resource_id in known_ids:
                dependencies[edge.to_resource_id].add(edge.from_resource_id)

    if all(not deps for deps in dependencies.values()):
        for resource in resources:
            for dep in resource.dependencies:
                if dep in known_ids:
                    dependencies[resource.resource_id].add(dep)

    dependents: dict[str, set[str]] = {resource_id: set() for resource_id in resource_ids}
    indegree: dict[str, int] = {resource_id: len(deps) for resource_id, deps in dependencies.items()}

    for target, deps in dependencies.items():
        for dep in deps:
            dependents[dep].add(target)

    available = [resource_id for resource_id, count in indegree.items() if count == 0]
    ordered: list[str] = []

    while available:
        available.sort(key=lambda item: (_resource_priority(item, resources), item.lower()))
        current = available.pop(0)
        ordered.append(current)

        for dependent in sorted(dependents[current], key=str.lower):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                available.append(dependent)

    if len(ordered) == len(resource_ids):
        return ordered

    remaining = [resource_id for resource_id in resource_ids if resource_id not in set(ordered)]
    remaining.sort(key=lambda item: (_resource_priority(item, resources), item.lower()))
    return [*ordered, *remaining]


def _resource_priority(resource_id: str, resources: list[ParsedResource]) -> int:
    lookup = {item.resource_id: item for item in resources}
    resource = lookup.get(resource_id)
    if resource is None:
        return 99

    source_type = resource.source_type.lower()
    if source_type.startswith("microsoft.network/"):
        return 0
    if source_type.startswith("microsoft.keyvault/"):
        return 1
    if source_type.startswith("microsoft.web/sites"):
        return 2
    if _references_vault_secret(resource.properties):
        return 3
    return 4


def _build_plan_item(
    *,
    resource: ParsedResource,
    mapping_result: MappingResult | None,
    artifact_flags_by_unit: dict[str, dict[str, bool]],
    manual_dependencies: list[str],
    thresholds: dict[str, int],
    security_flags: dict[str, bool],
) -> tuple[MigrationPlanItem, RiskScore]:
    notes: list[str] = []
    rationale: list[str] = []

    if mapping_result is None or not mapping_result.mapped:
        action = "Manual mapping and migration required"
        score = 100
        rationale.append("UNMAPPED resource requires manual intervention")
        notes.append("No supported mapping rule matched this resource")
    else:
        action = f"Deploy mapped construct: {mapping_result.target_resource_type}"
        confidence_score = int(round((1.0 - mapping_result.confidence) * 100))
        score = max(0, min(100, confidence_score))
        rationale.append(
            f"Mapping confidence from rule {mapping_result.rule_id}: {mapping_result.confidence:.2f}"
        )

    unit = _resolve_unit(resource)
    unit_flags = artifact_flags_by_unit.get(unit, {"wildcard_iam": False, "new_public": False})

    wildcard_enforced = security_flags.get("enforce_no_wildcard_iam", True)
    public_enforced = security_flags.get("enforce_no_new_public_exposure", True)

    hard_risk = False
    if wildcard_enforced and unit_flags["wildcard_iam"]:
        score = max(score, thresholds["high_min"])
        hard_risk = True
        rationale.append("Wildcard IAM permission detected in generated template")
    if public_enforced and unit_flags["new_public"]:
        score = max(score, thresholds["high_min"])
        hard_risk = True
        rationale.append("Potential new public exposure detected in generated template")

    if any(resource.resource_id in item for item in manual_dependencies):
        score = max(score, thresholds["review_max"] + 1)
        rationale.append("Manual dependency recorded for this resource")

    if resource.dependencies:
        notes.append("Depends on: " + ", ".join(sorted(resource.dependencies)))

    if hard_risk or score >= thresholds["high_min"]:
        status = PlanItemStatus.HIGH_RISK
    elif score <= thresholds["auto_max"]:
        status = PlanItemStatus.AUTO
    elif score <= thresholds["review_max"]:
        status = PlanItemStatus.REVIEW
    else:
        status = PlanItemStatus.HIGH_RISK

    if status == PlanItemStatus.REVIEW and not hard_risk:
        rationale.append("Requires operator review before deployment")

    notes.extend(rationale)

    plan_item = MigrationPlanItem(
        resource_id=resource.resource_id,
        action=action,
        status=status,
        risk_score=score,
        notes=notes,
    )
    risk_score = RiskScore(resource_id=resource.resource_id, score=score, rationale=rationale)
    return plan_item, risk_score


def _artifact_flags_by_unit(artifacts: list[CfnArtifact]) -> dict[str, dict[str, bool]]:
    flags: dict[str, dict[str, bool]] = defaultdict(
        lambda: {"wildcard_iam": False, "new_public": False}
    )

    for artifact in artifacts:
        unit = _artifact_unit(artifact)
        template_yaml = artifact.properties.get("template_yaml")
        if not isinstance(template_yaml, str) or not unit:
            continue

        try:
            template = yaml.safe_load(template_yaml)
        except yaml.YAMLError:
            continue

        if not isinstance(template, dict):
            continue

        wildcard = _contains_wildcard_iam(template)
        public = _contains_public_exposure(template)
        flags[unit]["wildcard_iam"] = flags[unit]["wildcard_iam"] or wildcard
        flags[unit]["new_public"] = flags[unit]["new_public"] or public

    return dict(flags)


def _artifact_unit(artifact: CfnArtifact) -> str:
    unit = artifact.properties.get("unit")
    if isinstance(unit, str) and unit:
        return unit

    logical_id = artifact.logical_id.lower()
    if "network" in logical_id:
        return "networking"
    if "function" in logical_id:
        return "functions"
    if "keyvault" in logical_id or "vault" in logical_id:
        return "keyvault"
    return "other"


def _resolve_unit(resource: ParsedResource) -> str:
    source_type = resource.source_type.lower()
    if source_type.startswith("microsoft.network/"):
        return "networking"
    if source_type.startswith("microsoft.web/sites"):
        return "functions"
    if source_type.startswith("microsoft.keyvault/"):
        return "keyvault"
    if _references_vault_secret(resource.properties):
        return "secret-consumer"
    return "other"


def _references_vault_secret(payload: dict[str, Any]) -> bool:
    text = json.dumps(payload, sort_keys=True, default=str).lower()
    markers = (
        "microsoft.keyvault",
        "@microsoft.keyvault",
        "secreturi",
        "secretsmanager",
        "keyvault",
        "vault.azure.net",
    )
    return any(marker in text for marker in markers)


def _contains_wildcard_iam(payload: Any) -> bool:
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if lowered in {"action", "resource"} and _value_has_wildcard(value):
                return True
            if _contains_wildcard_iam(value):
                return True
        return False

    if isinstance(payload, list):
        return any(_contains_wildcard_iam(item) for item in payload)

    return False


def _value_has_wildcard(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip() == "*"
    if isinstance(value, list):
        return any(isinstance(item, str) and item.strip() == "*" for item in value)
    return False


def _contains_public_exposure(payload: Any) -> bool:
    if isinstance(payload, dict):
        principal = payload.get("Principal")
        if principal == "*":
            return True
        if isinstance(principal, dict):
            if any(value == "*" for value in principal.values()):
                return True

        for value in payload.values():
            if _contains_public_exposure(value):
                return True
        return False

    if isinstance(payload, list):
        return any(_contains_public_exposure(item) for item in payload)

    if isinstance(payload, str):
        candidate = payload.strip().lower()
        return candidate in {"0.0.0.0/0", "::/0"}

    return False


def _build_plan_report(
    *,
    plan_items: list[MigrationPlanItem],
    parsed_by_id: dict[str, ParsedResource],
    owner: str,
    plan_hash: str,
) -> MigrationPlanReport:
    auto_count = sum(1 for item in plan_items if item.status == PlanItemStatus.AUTO)
    review_count = sum(1 for item in plan_items if item.status == PlanItemStatus.REVIEW)
    high_count = sum(1 for item in plan_items if item.status == PlanItemStatus.HIGH_RISK)

    markdown_summary = _render_markdown_report(plan_items, parsed_by_id, owner, plan_hash)
    html_summary = _render_html_report(plan_items, parsed_by_id, owner, plan_hash)

    return MigrationPlanReport(
        plan_hash=plan_hash,
        total_items=len(plan_items),
        auto_migratable_count=auto_count,
        requires_review_count=review_count,
        high_risk_count=high_count,
        markdown_summary=markdown_summary,
        html_summary=html_summary,
    )


def _render_markdown_report(
    plan_items: list[MigrationPlanItem],
    parsed_by_id: dict[str, ParsedResource],
    owner: str,
    plan_hash: str,
) -> str:
    lines = [
        f"Plan Hash: {plan_hash}",
        "",
        "| Category | Count |",
        "| --- | ---: |",
        f"| Auto-Migratable | {sum(1 for item in plan_items if item.status == PlanItemStatus.AUTO)} |",
        f"| Requires Review | {sum(1 for item in plan_items if item.status == PlanItemStatus.REVIEW)} |",
        f"| High Risk | {sum(1 for item in plan_items if item.status == PlanItemStatus.HIGH_RISK)} |",
        "",
        "| Stage | Resource | Sequence | Dependency | Owner |",
        "| --- | --- | ---: | --- | --- |",
    ]

    for index, item in enumerate(plan_items, start=1):
        resource = parsed_by_id.get(item.resource_id)
        dependency_cell = "-"
        if resource and resource.dependencies:
            dependency_cell = ", ".join(sorted(resource.dependencies))
        stage = "Deploy" if item.status != PlanItemStatus.HIGH_RISK else "ReviewGate"
        lines.append(f"| {stage} | {item.resource_id} | {index} | {dependency_cell} | {owner} |")

    lines.extend(["", "| Resource | Risk Level | Rationale | Required Action |", "| --- | --- | --- | --- |"])

    for item in plan_items:
        rationale = "; ".join(item.notes[-2:]) if item.notes else "-"
        required_action = "Manual remediation and approval" if item.status == PlanItemStatus.HIGH_RISK else (
            "Operator review" if item.status == PlanItemStatus.REVIEW else "Auto deploy"
        )
        lines.append(
            f"| {item.resource_id} | {item.status.value} | {rationale or '-'} | {required_action} |"
        )

    return "\n".join(lines)


def _render_html_report(
    plan_items: list[MigrationPlanItem],
    parsed_by_id: dict[str, ParsedResource],
    owner: str,
    plan_hash: str,
) -> str:
    auto_count = sum(1 for item in plan_items if item.status == PlanItemStatus.AUTO)
    review_count = sum(1 for item in plan_items if item.status == PlanItemStatus.REVIEW)
    high_count = sum(1 for item in plan_items if item.status == PlanItemStatus.HIGH_RISK)

    summary_rows = "".join(
        [
            f"<tr><td>Auto-Migratable</td><td>{auto_count}</td></tr>",
            f"<tr><td>Requires Review</td><td>{review_count}</td></tr>",
            f"<tr><td>High Risk</td><td>{high_count}</td></tr>",
        ]
    )

    plan_rows: list[str] = []
    risk_rows: list[str] = []
    for index, item in enumerate(plan_items, start=1):
        resource = parsed_by_id.get(item.resource_id)
        dependency_cell = "-"
        if resource and resource.dependencies:
            dependency_cell = ", ".join(sorted(resource.dependencies))
        stage = "Deploy" if item.status != PlanItemStatus.HIGH_RISK else "ReviewGate"
        plan_rows.append(
            "".join(
                [
                    "<tr>",
                    f"<td>{stage}</td>",
                    f"<td>{item.resource_id}</td>",
                    f"<td>{index}</td>",
                    f"<td>{dependency_cell}</td>",
                    f"<td>{owner}</td>",
                    "</tr>",
                ]
            )
        )

        rationale = "; ".join(item.notes[-2:]) if item.notes else "-"
        required_action = "Manual remediation and approval" if item.status == PlanItemStatus.HIGH_RISK else (
            "Operator review" if item.status == PlanItemStatus.REVIEW else "Auto deploy"
        )
        risk_rows.append(
            "".join(
                [
                    "<tr>",
                    f"<td>{item.resource_id}</td>",
                    f"<td>{item.status.value}</td>",
                    f"<td>{rationale}</td>",
                    f"<td>{required_action}</td>",
                    "</tr>",
                ]
            )
        )

    return "".join(
        [
            f"<p>Plan Hash: {plan_hash}</p>",
            "<table><thead><tr><th>Category</th><th>Count</th></tr></thead><tbody>",
            summary_rows,
            "</tbody></table>",
            "<table><thead><tr><th>Stage</th><th>Resource</th><th>Sequence</th><th>Dependency</th><th>Owner</th></tr></thead><tbody>",
            "".join(plan_rows),
            "</tbody></table>",
            "<table><thead><tr><th>Resource</th><th>Risk Level</th><th>Rationale</th><th>Required Action</th></tr></thead><tbody>",
            "".join(risk_rows),
            "</tbody></table>",
        ]
    )


def _compute_plan_hash(plan_items: list[MigrationPlanItem], artifacts: list[CfnArtifact]) -> str:
    ordered_plan = [
        {
            "resource_id": item.resource_id,
            "action": item.action,
            "status": item.status.value,
            "risk_score": item.risk_score,
            "notes": item.notes,
        }
        for item in plan_items
    ]

    ordered_templates = []
    for artifact in sorted(artifacts, key=lambda item: item.logical_id.lower()):
        properties = dict(artifact.properties)
        template_yaml = properties.get("template_yaml")
        if isinstance(template_yaml, str):
            try:
                template_obj = yaml.safe_load(template_yaml)
            except yaml.YAMLError:
                template_obj = template_yaml
            properties["template_yaml"] = template_obj

        ordered_templates.append(
            {
                "logical_id": artifact.logical_id,
                "resource_type": artifact.resource_type,
                "rule_ids_used": sorted(artifact.rule_ids_used),
                "properties": properties,
            }
        )

    return _stable_hash({"ordered_plan": ordered_plan, "generated_templates": ordered_templates})


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
