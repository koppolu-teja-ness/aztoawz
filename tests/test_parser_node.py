from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any

import pytest

from src.agents.parser import ParserRequest, parse_bicep_to_graph, parser_analyzer_node
from src.graph.state import MigrationState


_FIXTURE_ARM_TEMPLATES: dict[str, dict[str, Any]] = {
    "keyvault_sample.bicep": {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "vaultName": {"type": "string", "defaultValue": "kv-demo"},
            "location": {"type": "string", "defaultValue": "[resourceGroup().location]"},
        },
        "variables": {
            "secretName": "[concat(parameters('vaultName'), '-secret')]",
        },
        "resources": [
            {
                "type": "Microsoft.KeyVault/vaults",
                "apiVersion": "2023-07-01",
                "name": "[parameters('vaultName')]",
                "location": "[parameters('location')]",
                "properties": {
                    "enableRbacAuthorization": True,
                },
            }
        ],
    },
    "functionapp_sample.bicep": {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "functionAppName": {"type": "string", "defaultValue": "func-demo"},
            "storageAccountName": {"type": "string", "defaultValue": "stfuncdemo"},
            "location": {"type": "string", "defaultValue": "eastus"},
        },
        "variables": {
            "appInsightsName": "[concat(parameters('functionAppName'), '-appi')]",
        },
        "resources": [
            {
                "type": "Microsoft.Storage/storageAccounts",
                "apiVersion": "2023-05-01",
                "name": "[parameters('storageAccountName')]",
                "location": "[parameters('location')]",
                "properties": {},
            },
            {
                "type": "Microsoft.Web/sites",
                "apiVersion": "2022-09-01",
                "name": "[parameters('functionAppName')]",
                "location": "[parameters('location')]",
                "dependsOn": [
                    "[resourceId('Microsoft.Storage/storageAccounts', parameters('storageAccountName'))]"
                ],
                "properties": {
                    "siteConfig": {
                        "appSettings": [
                            {
                                "name": "APPINSIGHTS_NAME",
                                "value": "[variables('appInsightsName')]",
                            }
                        ]
                    }
                },
            },
        ],
    },
    "vnet_sample.bicep": {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {
            "vnetName": {"type": "string", "defaultValue": "vnet-demo"},
            "subnetName": {"type": "string", "defaultValue": "subnet-app"},
            "location": {"type": "string", "defaultValue": "eastus"},
        },
        "resources": [
            {
                "type": "Microsoft.Network/virtualNetworks",
                "apiVersion": "2023-11-01",
                "name": "[parameters('vnetName')]",
                "location": "[parameters('location')]",
                "properties": {
                    "addressSpace": {
                        "addressPrefixes": ["10.10.0.0/16"],
                    }
                },
            },
            {
                "type": "Microsoft.Network/virtualNetworks/subnets",
                "apiVersion": "2023-11-01",
                "name": "[concat(parameters('vnetName'), '/', parameters('subnetName'))]",
                "dependsOn": [
                    "[resourceId('Microsoft.Network/virtualNetworks', parameters('vnetName'))]"
                ],
                "properties": {
                    "addressPrefix": "10.10.1.0/24",
                },
            },
            {
                "type": "Microsoft.Resources/deployments",
                "apiVersion": "2022-09-01",
                "name": "diag-module",
                "dependsOn": [
                    "[resourceId('Microsoft.Network/virtualNetworks/subnets', parameters('vnetName'), parameters('subnetName'))]"
                ],
                "copy": {
                    "name": "diagLoop",
                    "count": 1,
                },
                "properties": {
                    "mode": "Incremental",
                    "templateLink": {
                        "uri": "[parameters('missingParam')]",
                    },
                },
            },
        ],
    },
}


def _fake_bicep_runner(command: list[str]) -> None:
    assert command[:3] == ["az", "bicep", "build"]
    assert "--file" in command
    assert "--outfile" in command

    file_index = command.index("--file") + 1
    outfile_index = command.index("--outfile") + 1

    source_name = Path(command[file_index]).name
    output_file = Path(command[outfile_index])

    payload = _FIXTURE_ARM_TEMPLATES[source_name]
    output_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _normalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(snapshot))

    normalized["audit_record"]["timestamp"] = "__TIMESTAMP__"
    normalized["audit_record"]["inputs_hash"] = "__INPUT_HASH__"
    normalized["audit_record"]["outputs"]["artifact_paths"]["compiled_arm"] = "__COMPILED_ARM__"
    normalized["audit_record"]["outputs"]["artifact_paths"]["parsed_summary"] = "__PARSED_SUMMARY__"

    for resource in normalized["parsed_resources"]:
        resource["properties"]["origin_ref"] = _normalize_origin(resource["properties"]["origin_ref"])

    for node in normalized["dependency_graph"]["nodes"]:
        node["origin_ref"] = _normalize_origin(node["origin_ref"])

    return normalized


def _normalize_origin(origin_ref: str) -> str:
    stable = re.sub(r"sha256:[a-f0-9]{64}\.arm\.json", "__ARM_ARTIFACT__.arm.json", origin_ref)
    if "#/" not in stable:
        return "__ARM_ORIGIN__"
    _, suffix = stable.split("#/", maxsplit=1)
    return f"__ARM_ORIGIN__#/{suffix}"


def _fixture_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"


@pytest.mark.parametrize(
    "fixture_name,expected_name",
    [
        ("keyvault_sample.bicep", "parser_keyvault_expected.json"),
        ("functionapp_sample.bicep", "parser_functionapp_expected.json"),
        ("vnet_sample.bicep", "parser_vnet_expected.json"),
    ],
)
def test_parse_bicep_to_graph_matches_golden_snapshot(
    fixture_name: str,
    expected_name: str,
    tmp_path: Path,
) -> None:
    fixture_path = _fixture_dir() / "bicep" / fixture_name

    result = parse_bicep_to_graph(
        ParserRequest(bicep_path=str(fixture_path), artifacts_dir=str(tmp_path / "artifacts")),
        command_runner=_fake_bicep_runner,
        now_provider=lambda: datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
        run_id="run-parser-001",
    )

    normalized = _normalize_snapshot(result.model_dump(mode="json"))

    expected_path = _fixture_dir() / "expected" / expected_name
    if os.getenv("UPDATE_GOLDENS") == "1":
        expected_path.write_text(json.dumps(normalized, indent=2, sort_keys=True), encoding="utf-8")
    expected = json.loads(expected_path.read_text(encoding="utf-8"))

    assert normalized == expected


def test_parser_analyzer_node_updates_state_with_graph(tmp_path: Path) -> None:
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    state = MigrationState(
        run_id="run-parser-002",
        started_at=now,
        updated_at=now,
    )

    fixture_path = _fixture_dir() / "bicep" / "functionapp_sample.bicep"
    updated = parser_analyzer_node(
        state,
        ParserRequest(bicep_path=str(fixture_path), artifacts_dir=str(tmp_path / "artifacts")),
        command_runner=_fake_bicep_runner,
        now_provider=lambda: now,
    )

    assert len(updated.parsed_resources) == 2
    assert updated.parsed_dependency_graph is not None
    assert len(updated.parsed_dependency_graph.nodes) == 2
    assert len(updated.parsed_dependency_graph.edges) == 1
    assert len(updated.audit_records) == 1
    assert updated.audit_records[0].outputs["stage"] == "parser"
