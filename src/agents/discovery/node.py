"""Discovery agent node for enumerating Azure source resources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.graph.state import AuditRecord, DiscoveredResource, MigrationState


class DiscoveryRequest(BaseModel):
    """Input contract for discovery stage execution."""

    model_config = ConfigDict(extra="forbid")

    bicep_path: str | None = None
    use_live_azure: bool = False
    subscription_id: str | None = None
    resource_group: str | None = None

    @model_validator(mode="after")
    def validate_source_selector(self) -> "DiscoveryRequest":
        if not self.bicep_path and not self.use_live_azure:
            raise ValueError("Set bicep_path and/or use_live_azure for discovery input")
        return self


CommandRunner = Callable[[list[str]], str]
AzureResourceLister = Callable[[str | None, str | None], list[dict[str, Any]]]


@dataclass(frozen=True)
class _ParsedBicepResource:
    """Minimal Bicep resource declaration extracted from source file."""

    symbol_name: str
    resource_type: str
    name: str
    region: str
    tags: dict[str, str]
    raw_properties: dict[str, Any]


def discovery_node(
    state: MigrationState,
    request: DiscoveryRequest,
    *,
    command_runner: CommandRunner | None = None,
    azure_resource_lister: AzureResourceLister | None = None,
) -> MigrationState:
    """Run discovery and emit updated state plus stage audit record."""

    discovered: list[DiscoveredResource] = []
    manual_dependencies: list[str] = []

    if request.use_live_azure:
        live_resources = _list_live_azure_resources(
            subscription_id=request.subscription_id,
            resource_group=request.resource_group,
            command_runner=command_runner,
            azure_resource_lister=azure_resource_lister,
        )
        live_discovered, live_manual = _classify_raw_resources(live_resources)
        discovered.extend(live_discovered)
        manual_dependencies.extend(live_manual)

    if request.bicep_path:
        bicep_discovered, bicep_manual = _discover_from_bicep_path(Path(request.bicep_path))
        discovered.extend(bicep_discovered)
        manual_dependencies.extend(bicep_manual)

    discovered = _dedupe_resources(discovered)
    merged_manual_dependencies = _merge_unique(state.manual_dependencies, manual_dependencies)

    now = datetime.now(tz=timezone.utc)
    audit = AuditRecord(
        inputs_hash=_stable_hash(
            {
                "run_id": state.run_id,
                "request": request.model_dump(mode="json"),
            }
        ),
        outputs={
            "stage": "discovery",
            "discovered_count": len(discovered),
            "manual_dependency_count": len(merged_manual_dependencies),
            "discovered_resource_types": sorted({item.resource_type for item in discovered}),
            "sources": {
                "live_azure": request.use_live_azure,
                "bicep_path": bool(request.bicep_path),
            },
        },
        rule_ids_used=[],
        reviewer=None,
        timestamp=now,
    )

    return state.model_copy(
        update={
            "updated_at": now,
            "discovered_resources": discovered,
            "manual_dependencies": merged_manual_dependencies,
            "audit_records": [*state.audit_records, audit],
        }
    )


def _list_live_azure_resources(
    *,
    subscription_id: str | None,
    resource_group: str | None,
    command_runner: CommandRunner | None,
    azure_resource_lister: AzureResourceLister | None,
) -> list[dict[str, Any]]:
    if azure_resource_lister is not None:
        return azure_resource_lister(subscription_id, resource_group)

    runner = command_runner or _run_az_cli_command
    command = ["az", "resource", "list", "--output", "json"]

    if subscription_id:
        command.extend(["--subscription", subscription_id])
    if resource_group:
        command.extend(["--resource-group", resource_group])

    payload = runner(command)
    parsed = json.loads(payload)
    if not isinstance(parsed, list):
        raise ValueError("Azure CLI response must be a JSON list")

    typed: list[dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict):
            typed.append(item)
    return typed


def _run_az_cli_command(command: list[str]) -> str:
    resolved = _resolve_command(command)
    completed = subprocess.run(resolved, check=True, capture_output=True, text=True)
    return completed.stdout


def _resolve_command(command: list[str]) -> list[str]:
    """Resolve the executable (e.g. az.cmd on Windows) so subprocess can find it without a shell."""

    resolved_executable = shutil.which(command[0])
    if resolved_executable is None:
        return command
    return [resolved_executable, *command[1:]]


def _classify_raw_resources(
    raw_resources: list[dict[str, Any]],
) -> tuple[list[DiscoveredResource], list[str]]:
    discovered: list[DiscoveredResource] = []
    manual_dependencies: list[str] = []

    for item in raw_resources:
        resource_type = str(item.get("type", ""))
        name = str(item.get("name", ""))

        if _is_in_scope_resource(resource_type, raw=item):
            discovered.append(
                DiscoveredResource(
                    resource_id=str(item.get("id", f"azure:{resource_type}:{name}")),
                    resource_type=resource_type,
                    name=name,
                    resource_group=_coerce_optional_str(item.get("resourceGroup")),
                    region=_coerce_optional_str(item.get("location")) or "unknown",
                    tags=_coerce_tags(item.get("tags")),
                    raw_properties=_extract_raw_properties(item),
                )
            )
        else:
            manual_dependencies.append(
                f"Out-of-scope resource type requires manual handling: {resource_type} ({name})"
            )

    return discovered, manual_dependencies


def _discover_from_bicep_path(path: Path) -> tuple[list[DiscoveredResource], list[str]]:
    files = _resolve_bicep_files(path)

    discovered: list[DiscoveredResource] = []
    manual_dependencies: list[str] = []

    for bicep_file in files:
        content = bicep_file.read_text(encoding="utf-8")
        parsed_resources = _parse_bicep_resources(content)

        for parsed in parsed_resources:
            if _is_in_scope_resource(parsed.resource_type, raw=parsed.raw_properties):
                discovered.append(
                    DiscoveredResource(
                        resource_id=(
                            f"bicep:{bicep_file.as_posix()}:{parsed.resource_type}:{parsed.symbol_name}"
                        ),
                        resource_type=parsed.resource_type,
                        name=parsed.name,
                        resource_group=None,
                        region=parsed.region,
                        tags=parsed.tags,
                        raw_properties=parsed.raw_properties,
                    )
                )
            else:
                manual_dependencies.append(
                    "Out-of-scope resource type requires manual handling: "
                    f"{parsed.resource_type} ({parsed.name})"
                )

    return discovered, manual_dependencies


def _resolve_bicep_files(path: Path) -> list[Path]:
    if path.is_file() and path.suffix.lower() == ".bicep":
        return [path]
    if path.is_dir():
        return sorted(path.rglob("*.bicep"))
    raise FileNotFoundError(f"No Bicep file or directory found at {path}")


def _parse_bicep_resources(content: str) -> list[_ParsedBicepResource]:
    resources: list[_ParsedBicepResource] = []
    declarations = re.finditer(
        r"resource\s+([A-Za-z_][A-Za-z0-9_]*)\s+'([^'@]+)(?:@[^']+)?'\s*=\s*\{",
        content,
        flags=re.IGNORECASE,
    )

    for match in declarations:
        symbol_name = match.group(1)
        resource_type = match.group(2)

        block_start = match.end() - 1
        block = _extract_brace_block(content, block_start)

        name = _extract_bicep_value(block, "name") or symbol_name
        region = _extract_bicep_value(block, "location") or "unknown"
        kind = _extract_bicep_value(block, "kind")
        tags = _extract_bicep_tags(block)

        raw_properties: dict[str, Any] = {
            "bicep_block": block,
        }
        if kind is not None:
            raw_properties["kind"] = kind

        resources.append(
            _ParsedBicepResource(
                symbol_name=symbol_name,
                resource_type=resource_type,
                name=name,
                region=region,
                tags=tags,
                raw_properties=raw_properties,
            )
        )

    return resources


def _extract_brace_block(content: str, start_index: int) -> str:
    depth = 0
    for index in range(start_index, len(content)):
        char = content[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[start_index : index + 1]
    raise ValueError("Unbalanced Bicep resource block braces")


def _extract_bicep_value(resource_block: str, key: str) -> str | None:
    pattern = rf"\b{re.escape(key)}\s*:\s*([^\n\r]+)"
    match = re.search(pattern, resource_block)
    if not match:
        return None

    raw_value = match.group(1).strip().rstrip(",")
    return raw_value.strip("\"'")


def _extract_bicep_tags(resource_block: str) -> dict[str, str]:
    tags_match = re.search(r"\btags\s*:\s*\{", resource_block)
    if not tags_match:
        return {}

    block = _extract_brace_block(resource_block, tags_match.end() - 1)
    tags: dict[str, str] = {}

    for line in block.splitlines():
        pair = re.match(r"\s*([A-Za-z0-9_\-\"']+)\s*:\s*(.+?)\s*$", line)
        if not pair:
            continue
        key = pair.group(1).strip().strip("\"'")
        value = pair.group(2).strip().rstrip(",").strip("\"'")
        if key and key.lower() != "tags" and value and value != "{":
            tags[key] = value

    return tags


def _is_in_scope_resource(resource_type: str, raw: dict[str, Any] | None = None) -> bool:
    lowered = resource_type.lower()

    if lowered.startswith("microsoft.keyvault/vaults"):
        return True

    if lowered.startswith("microsoft.web/sites"):
        if lowered != "microsoft.web/sites":
            return True

        kind = ""
        if raw is not None:
            kind = str(raw.get("kind", ""))
            if not kind:
                kind = str(_extract_raw_properties(raw).get("kind", ""))
        return "functionapp" in kind.lower()

    network_prefixes = (
        "microsoft.network/virtualnetworks",
        "microsoft.network/networksecuritygroups",
        "microsoft.network/routetables",
        "microsoft.network/privateendpoints",
    )

    return lowered.startswith(network_prefixes)


def _extract_raw_properties(raw_resource: dict[str, Any]) -> dict[str, Any]:
    raw_properties = raw_resource.get("properties")
    if isinstance(raw_properties, dict):
        result = dict(raw_properties)
    else:
        result = {}

    for key in ("kind", "sku", "identity"):
        value = raw_resource.get(key)
        if value is not None:
            result[key] = value

    return result


def _coerce_tags(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}

    tags: dict[str, str] = {}
    for key, item in value.items():
        tags[str(key)] = str(item)
    return tags


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    coerced = str(value)
    return coerced if coerced else None


def _dedupe_resources(resources: list[DiscoveredResource]) -> list[DiscoveredResource]:
    by_id: dict[str, DiscoveredResource] = {}
    for resource in resources:
        by_id[resource.resource_id] = resource
    return list(by_id.values())


def _merge_unique(existing: list[str], discovered: list[str]) -> list[str]:
    ordered_unique: list[str] = []
    for item in [*existing, *discovered]:
        if item not in ordered_unique:
            ordered_unique.append(item)
    return ordered_unique


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
