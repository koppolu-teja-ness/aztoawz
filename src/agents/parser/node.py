"""Parser/analyzer stage for Bicep to ARM resource graph extraction."""

from __future__ import annotations

from datetime import datetime, timezone
from graphlib import CycleError, TopologicalSorter
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from src.graph.state import (
    AuditRecord,
    MigrationState,
    ParsedResource,
    ResourceDependencyGraph,
    ResourceGraphEdge,
    ResourceGraphNode,
)


class ParserRequest(BaseModel):
    """Input contract for parser/analyzer stage."""

    model_config = ConfigDict(extra="forbid")

    bicep_path: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    artifacts_dir: str | None = None


class UnsupportedConstruct(BaseModel):
    """Unsupported or unresolved construct requiring manual handling."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    reason: str
    location: str
    impact: str


class ParseDiagnostic(BaseModel):
    """Deterministic diagnostic emitted during parser/analyzer execution."""

    model_config = ConfigDict(extra="forbid")

    severity: str
    code: str
    message: str
    location: str


class ParserResult(BaseModel):
    """Parser/analyzer outputs for downstream stages."""

    model_config = ConfigDict(extra="forbid")

    parsed_resources: list[ParsedResource] = Field(default_factory=list)
    dependency_graph: ResourceDependencyGraph
    unsupported_constructs: list[UnsupportedConstruct] = Field(default_factory=list)
    diagnostics: list[ParseDiagnostic] = Field(default_factory=list)
    audit_record: AuditRecord


CommandRunner = Callable[[list[str]], None]


def parser_analyzer_node(
    state: MigrationState,
    request: ParserRequest,
    *,
    command_runner: CommandRunner | None = None,
    now_provider: Callable[[], datetime] | None = None,
) -> MigrationState:
    """Run parser/analyzer and merge outputs into shared migration state."""

    result = parse_bicep_to_graph(
        request,
        command_runner=command_runner,
        now_provider=now_provider,
        run_id=state.run_id,
    )

    manual_items = [
        f"Unsupported construct: {item.kind} at {item.location} ({item.impact})"
        for item in result.unsupported_constructs
    ]
    merged_manual_dependencies = _merge_unique(state.manual_dependencies, manual_items)

    return state.model_copy(
        update={
            "updated_at": result.audit_record.timestamp,
            "parsed_resources": result.parsed_resources,
            "parsed_dependency_graph": result.dependency_graph,
            "manual_dependencies": merged_manual_dependencies,
            "audit_records": [*state.audit_records, result.audit_record],
        }
    )


def parse_bicep_to_graph(
    request: ParserRequest,
    *,
    command_runner: CommandRunner | None = None,
    now_provider: Callable[[], datetime] | None = None,
    run_id: str = "standalone-parse",
) -> ParserResult:
    """Compile Bicep to ARM, resolve dependencies, and emit typed parser outputs."""

    runner = command_runner or _run_command
    now_fn = now_provider or (lambda: datetime.now(tz=timezone.utc))

    bicep_path = Path(request.bicep_path).resolve()
    if not bicep_path.exists() or not bicep_path.is_file():
        raise FileNotFoundError(f"Bicep path not found: {bicep_path}")

    bicep_source = bicep_path.read_text(encoding="utf-8")
    input_hash = _stable_hash(
        {
            "bicep_source": bicep_source,
            "parameters": request.parameters,
            "run_id": run_id,
        }
    )

    artifacts_dir = (
        Path(request.artifacts_dir).resolve()
        if request.artifacts_dir
        else bicep_path.parent / ".artifacts" / "parser"
    )
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    arm_output_path = artifacts_dir / f"{input_hash}.arm.json"
    parsed_output_path = artifacts_dir / f"{input_hash}.parsed.json"

    _compile_bicep(bicep_path=bicep_path, arm_output_path=arm_output_path, runner=runner)

    template = json.loads(arm_output_path.read_text(encoding="utf-8"))
    if not isinstance(template, dict):
        raise ValueError("Compiled ARM JSON must deserialize into an object")

    resolved_parameters = _resolve_parameters(template=template, overrides=request.parameters)
    resolved_variables, variable_diagnostics = _resolve_variables(
        template=template,
        parameters=resolved_parameters,
    )

    parsed_resources, graph, unsupported, diagnostics = _extract_resources_and_graph(
        template=template,
        arm_output_path=arm_output_path,
        parameters=resolved_parameters,
        variables=resolved_variables,
    )
    diagnostics.extend(variable_diagnostics)

    _check_for_cycles(graph=graph, diagnostics=diagnostics)

    audit_timestamp = now_fn()
    audit_record = AuditRecord(
        inputs_hash=input_hash,
        outputs={
            "stage": "parser",
            "parsed_count": len(parsed_resources),
            "graph_node_count": len(graph.nodes),
            "graph_edge_count": len(graph.edges),
            "unsupported_count": len(unsupported),
            "diagnostic_count": len(diagnostics),
            "artifact_paths": {
                "compiled_arm": arm_output_path.as_posix(),
                "parsed_summary": parsed_output_path.as_posix(),
            },
        },
        rule_ids_used=[],
        reviewer=None,
        timestamp=audit_timestamp,
    )

    result = ParserResult(
        parsed_resources=parsed_resources,
        dependency_graph=graph,
        unsupported_constructs=unsupported,
        diagnostics=diagnostics,
        audit_record=audit_record,
    )

    parsed_output_path.write_text(
        json.dumps(
            {
                "parsed_resources": [resource.model_dump(mode="json") for resource in parsed_resources],
                "dependency_graph": graph.model_dump(mode="json"),
                "unsupported_constructs": [item.model_dump(mode="json") for item in unsupported],
                "diagnostics": [item.model_dump(mode="json") for item in diagnostics],
                "inputs_hash": input_hash,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    return result


def _compile_bicep(*, bicep_path: Path, arm_output_path: Path, runner: CommandRunner) -> None:
    command = [
        "az",
        "bicep",
        "build",
        "--file",
        str(bicep_path),
        "--outfile",
        str(arm_output_path),
    ]
    runner(command)


def _run_command(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


def _resolve_parameters(template: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    raw_parameters = template.get("parameters", {})
    if not isinstance(raw_parameters, dict):
        return resolved

    for key, payload in raw_parameters.items():
        if key in overrides:
            resolved[key] = overrides[key]
            continue
        if isinstance(payload, dict) and "defaultValue" in payload:
            resolved[key] = payload["defaultValue"]
            continue
        resolved[key] = None
    return resolved


def _resolve_variables(
    *,
    template: dict[str, Any],
    parameters: dict[str, Any],
) -> tuple[dict[str, Any], list[ParseDiagnostic]]:
    raw_variables = template.get("variables", {})
    if not isinstance(raw_variables, dict):
        return {}, []

    unresolved = dict(raw_variables)
    resolved: dict[str, Any] = {}
    diagnostics: list[ParseDiagnostic] = []

    for _ in range(max(len(unresolved), 1) * 2):
        if not unresolved:
            break
        progress = False
        for var_name in list(unresolved):
            value = unresolved[var_name]
            resolved_value, unresolved_tokens = _resolve_value(
                value,
                parameters=parameters,
                variables={**resolved},
            )
            if unresolved_tokens:
                continue
            resolved[var_name] = resolved_value
            unresolved.pop(var_name)
            progress = True
        if not progress:
            break

    for var_name in sorted(unresolved):
        diagnostics.append(
            ParseDiagnostic(
                severity="ERROR",
                code="UNRESOLVED_VARIABLE",
                message=f"Unable to resolve variable '{var_name}'",
                location=f"template.variables.{var_name}",
            )
        )

    return resolved, diagnostics


def _extract_resources_and_graph(
    *,
    template: dict[str, Any],
    arm_output_path: Path,
    parameters: dict[str, Any],
    variables: dict[str, Any],
) -> tuple[
    list[ParsedResource],
    ResourceDependencyGraph,
    list[UnsupportedConstruct],
    list[ParseDiagnostic],
]:
    raw_resources = template.get("resources", [])
    if not isinstance(raw_resources, list):
        raise ValueError("template.resources must be a list")

    flattened = _flatten_resources(raw_resources, prefix="template.resources")

    parsed_resources: list[ParsedResource] = []
    nodes: list[ResourceGraphNode] = []
    unresolved_diagnostics: list[ParseDiagnostic] = []
    unsupported: list[UnsupportedConstruct] = []

    index_to_resource_id: dict[int, str] = {}
    raw_depends_on: dict[int, list[str]] = {}

    for index, entry in enumerate(flattened):
        resource = entry["resource"]
        location = entry["location"]

        if not isinstance(resource, dict):
            unsupported.append(
                UnsupportedConstruct(
                    kind="non_object_resource",
                    reason="Resource entry is not an object",
                    location=location,
                    impact="Cannot parse resource for migration",
                )
            )
            continue

        if "copy" in resource:
            unsupported.append(
                UnsupportedConstruct(
                    kind="copy_loop",
                    reason="Resource copy loops are not auto-resolved",
                    location=location,
                    impact="Manual handling required for deterministic expansion",
                )
            )

        resource_type, type_tokens = _resolve_scalar(resource.get("type"), parameters, variables)
        resource_name, name_tokens = _resolve_scalar(resource.get("name"), parameters, variables)
        scope, _ = _resolve_scalar(resource.get("scope"), parameters, variables)

        if type_tokens:
            unresolved_diagnostics.append(
                ParseDiagnostic(
                    severity="ERROR",
                    code="UNRESOLVED_RESOURCE_TYPE",
                    message=f"Unable to resolve resource type at {location}",
                    location=f"{location}.type",
                )
            )
        if name_tokens:
            unresolved_diagnostics.append(
                ParseDiagnostic(
                    severity="ERROR",
                    code="UNRESOLVED_RESOURCE_NAME",
                    message=f"Unable to resolve resource name at {location}",
                    location=f"{location}.name",
                )
            )

        resource_type_value = str(resource_type) if resource_type is not None else "unknown-type"
        resource_name_value = str(resource_name) if resource_name is not None else f"unknown-name-{index}"

        resource_id = f"{resource_type_value}/{resource_name_value}"
        index_to_resource_id[index] = resource_id

        resource_properties = resource.get("properties", {})
        resolved_properties, unresolved_tokens = _resolve_value(
            resource_properties,
            parameters=parameters,
            variables=variables,
        )
        for token in unresolved_tokens:
            unresolved_diagnostics.append(
                ParseDiagnostic(
                    severity="WARN",
                    code="UNRESOLVED_EXPRESSION",
                    message=f"Unresolved expression token '{token}' in {location}.properties",
                    location=f"{location}.properties",
                )
            )

        origin_ref = f"{arm_output_path.as_posix()}#/{location.replace('.', '/')}"

        parsed_resources.append(
            ParsedResource(
                resource_id=resource_id,
                source_type=resource_type_value,
                normalized_type=_normalize_type(resource_type_value),
                properties={
                    "resolved_properties": resolved_properties,
                    "origin_ref": origin_ref,
                },
                dependencies=[],
            )
        )

        nodes.append(
            ResourceGraphNode(
                resource_id=resource_id,
                source_type=resource_type_value,
                name=resource_name_value,
                scope=str(scope or "resourceGroup"),
                origin_ref=origin_ref,
            )
        )

        depends = resource.get("dependsOn", [])
        if isinstance(depends, list):
            raw_depends_on[index] = [str(item) for item in depends]
        else:
            raw_depends_on[index] = []
            unsupported.append(
                UnsupportedConstruct(
                    kind="invalid_dependsOn",
                    reason="dependsOn must be a list",
                    location=f"{location}.dependsOn",
                    impact="Dependency ordering may be incomplete",
                )
            )

        if str(resource.get("type", "")).lower() == "microsoft.resources/deployments":
            unsupported.append(
                UnsupportedConstruct(
                    kind="nested_deployment_or_module",
                    reason="Compiled module/nested deployment requires manual resolution",
                    location=location,
                    impact="Nested template mapping is out of current automated scope",
                )
            )

    resource_lookup = {node.resource_id: node.resource_id for node in nodes}
    edges: list[ResourceGraphEdge] = []

    for index, deps in raw_depends_on.items():
        target_resource_id = index_to_resource_id.get(index)
        if target_resource_id is None:
            continue

        parsed_dependency_ids: list[str] = []
        for dep in deps:
            dep_resource_id, resolved = _parse_dependency_reference(
                dep,
                parameters=parameters,
                variables=variables,
            )
            if dep_resource_id is None:
                unresolved_diagnostics.append(
                    ParseDiagnostic(
                        severity="WARN",
                        code="UNRESOLVED_DEPENDENCY",
                        message=f"Unable to resolve dependency '{dep}'",
                        location=f"resource[{index}].dependsOn",
                    )
                )
                continue

            parsed_dependency_ids.append(dep_resource_id)
            if dep_resource_id not in resource_lookup:
                unsupported.append(
                    UnsupportedConstruct(
                        kind="external_dependency",
                        reason="Dependency target not present in compiled resource set",
                        location=f"resource[{index}].dependsOn",
                        impact="Manual verification needed for cross-template dependency",
                    )
                )
                continue

            edges.append(
                ResourceGraphEdge(
                    from_resource_id=dep_resource_id,
                    to_resource_id=target_resource_id,
                    reason="dependsOn_expression" if not resolved else "dependsOn",
                )
            )

        for parsed_resource in parsed_resources:
            if parsed_resource.resource_id == target_resource_id:
                parsed_resource.dependencies = parsed_dependency_ids
                break

    graph = ResourceDependencyGraph(nodes=nodes, edges=edges)
    return parsed_resources, graph, unsupported, unresolved_diagnostics


def _flatten_resources(
    resources: list[Any],
    *,
    prefix: str,
) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []

    for index, resource in enumerate(resources):
        location = f"{prefix}[{index}]"
        flat.append({"resource": resource, "location": location})
        if isinstance(resource, dict):
            nested = resource.get("resources")
            if isinstance(nested, list):
                flat.extend(_flatten_resources(nested, prefix=f"{location}.resources"))

    return flat


def _resolve_scalar(
    value: Any,
    parameters: dict[str, Any],
    variables: dict[str, Any],
) -> tuple[Any, list[str]]:
    resolved, unresolved = _resolve_value(value, parameters=parameters, variables=variables)
    return resolved, unresolved


def _resolve_value(
    value: Any,
    *,
    parameters: dict[str, Any],
    variables: dict[str, Any],
) -> tuple[Any, list[str]]:
    if isinstance(value, dict):
        unresolved: list[str] = []
        resolved_dict: dict[str, Any] = {}
        for key, nested in value.items():
            resolved_item, item_unresolved = _resolve_value(
                nested,
                parameters=parameters,
                variables=variables,
            )
            resolved_dict[str(key)] = resolved_item
            unresolved.extend(item_unresolved)
        return resolved_dict, unresolved

    if isinstance(value, list):
        unresolved: list[str] = []
        resolved_list: list[Any] = []
        for item in value:
            resolved_item, item_unresolved = _resolve_value(
                item,
                parameters=parameters,
                variables=variables,
            )
            resolved_list.append(resolved_item)
            unresolved.extend(item_unresolved)
        return resolved_list, unresolved

    if not isinstance(value, str):
        return value, []

    expression_match = re.fullmatch(r"\[\s*(.+?)\s*\]", value)
    if not expression_match:
        return value, []

    expr = expression_match.group(1)

    param_match = re.fullmatch(r"parameters\('([^']+)'\)", expr, flags=re.IGNORECASE)
    if param_match:
        param_name = param_match.group(1)
        if param_name in parameters:
            return parameters[param_name], []
        return value, [f"parameters:{param_name}"]

    var_match = re.fullmatch(r"variables\('([^']+)'\)", expr, flags=re.IGNORECASE)
    if var_match:
        variable_name = var_match.group(1)
        if variable_name in variables:
            return variables[variable_name], []
        return value, [f"variables:{variable_name}"]

    concat_match = re.fullmatch(r"concat\((.*)\)", expr, flags=re.IGNORECASE)
    if concat_match:
        parts = _split_function_args(concat_match.group(1))
        unresolved: list[str] = []
        resolved_parts: list[str] = []
        for part in parts:
            token = part.strip()
            if token.startswith("'") and token.endswith("'"):
                resolved_parts.append(token[1:-1])
                continue

            resolved_token, token_unresolved = _resolve_value(
                f"[{token}]",
                parameters=parameters,
                variables=variables,
            )
            if token_unresolved:
                unresolved.extend(token_unresolved)
            resolved_parts.append(str(resolved_token))

        if unresolved:
            return value, unresolved
        return "".join(resolved_parts), []

    return value, [expr]


def _split_function_args(args_blob: str) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    in_quote = False
    depth = 0

    for char in args_blob:
        if char == "'":
            in_quote = not in_quote
            current.append(char)
            continue

        if not in_quote:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                args.append("".join(current).strip())
                current = []
                continue

        current.append(char)

    tail = "".join(current).strip()
    if tail:
        args.append(tail)

    return args


def _parse_dependency_reference(
    dependency: str,
    *,
    parameters: dict[str, Any],
    variables: dict[str, Any],
) -> tuple[str | None, bool]:
    expression_match = re.fullmatch(r"\[\s*(.+?)\s*\]", dependency)
    if not expression_match:
        return dependency, True

    expr = expression_match.group(1)

    resource_id_match = re.fullmatch(r"resourceId\((.*)\)", expr, flags=re.IGNORECASE)
    if resource_id_match:
        parts = _split_function_args(resource_id_match.group(1))
        if len(parts) < 2:
            return None, False

        type_literal = _strip_quotes(parts[0])
        if type_literal is None:
            return None, False

        names: list[str] = []
        for part in parts[1:]:
            part = part.strip()
            literal = _strip_quotes(part)
            if literal is not None:
                names.append(literal)
                continue

            resolved, unresolved = _resolve_value(
                f"[{part}]",
                parameters=parameters,
                variables=variables,
            )
            if unresolved:
                return None, False
            names.append(str(resolved))

        if not names:
            return None, False

        return f"{type_literal}/{'/'.join(names)}", True

    resolved, unresolved = _resolve_value(
        dependency,
        parameters=parameters,
        variables=variables,
    )
    if unresolved:
        return None, False

    return str(resolved), False


def _strip_quotes(value: str) -> str | None:
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == "'" and trimmed[-1] == "'":
        return trimmed[1:-1]
    return None


def _check_for_cycles(
    *,
    graph: ResourceDependencyGraph,
    diagnostics: list[ParseDiagnostic],
) -> None:
    adjacency: dict[str, set[str]] = {node.resource_id: set() for node in graph.nodes}
    for edge in graph.edges:
        adjacency.setdefault(edge.to_resource_id, set())
        adjacency[edge.to_resource_id].add(edge.from_resource_id)

    try:
        sorter = TopologicalSorter(adjacency)
        sorter.prepare()
    except CycleError as exc:
        diagnostics.append(
            ParseDiagnostic(
                severity="ERROR",
                code="CYCLIC_DEPENDENCY",
                message="Cyclic dependency detected in parsed resource graph",
                location=str(exc.args[1]) if len(exc.args) > 1 else "template.resources",
            )
        )


def _normalize_type(resource_type: str) -> str:
    return resource_type.lower().replace(".", "_").replace("/", "_")


def _merge_unique(existing: list[str], incoming: list[str]) -> list[str]:
    merged = list(existing)
    seen = set(existing)
    for item in incoming:
        if item in seen:
            continue
        merged.append(item)
        seen.add(item)
    return merged


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
