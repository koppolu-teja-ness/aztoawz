from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from src.agents.discovery import DiscoveryRequest, discovery_node
from src.graph.state import MigrationState


def _base_state() -> MigrationState:
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    return MigrationState(
        run_id="run-discovery-001",
        started_at=now,
        updated_at=now,
    )


def test_discovery_node_parses_bicep_and_flags_out_of_scope() -> None:
    fixture_file = (
        Path(__file__).resolve().parent / "fixtures" / "discovery" / "sample_resources.bicep"
    )

    updated = discovery_node(
        _base_state(),
        DiscoveryRequest(bicep_path=str(fixture_file)),
    )

    discovered_types = {resource.resource_type for resource in updated.discovered_resources}
    discovered_names = {resource.name for resource in updated.discovered_resources}

    assert "Microsoft.KeyVault/vaults" in discovered_types
    assert "Microsoft.Web/sites" in discovered_types
    assert "Microsoft.Network/virtualNetworks" in discovered_types
    assert "Microsoft.Network/virtualNetworks/subnets" in discovered_types

    assert "kv-demo" in discovered_names
    assert "func-demo" in discovered_names
    assert "vnet-demo" in discovered_names

    assert all(
        "Microsoft.Storage/storageAccounts" in dependency
        for dependency in updated.manual_dependencies
    )

    assert len(updated.audit_records) == 1
    assert updated.audit_records[0].outputs["stage"] == "discovery"
    assert updated.audit_records[0].outputs["discovered_count"] == 4


def test_discovery_node_uses_mocked_azure_lister_and_filters_scope() -> None:
    mocked_lister = Mock(
        return_value=[
            {
                "id": "/subscriptions/sub-001/resourceGroups/rg-a/providers/Microsoft.KeyVault/vaults/kv-prod",
                "type": "Microsoft.KeyVault/vaults",
                "name": "kv-prod",
                "resourceGroup": "rg-a",
                "location": "eastus",
                "tags": {"owner": "platform"},
                "properties": {"enableRbacAuthorization": True},
            },
            {
                "id": "/subscriptions/sub-001/resourceGroups/rg-a/providers/Microsoft.Sql/servers/sql-demo",
                "type": "Microsoft.Sql/servers",
                "name": "sql-demo",
                "resourceGroup": "rg-a",
                "location": "eastus2",
                "tags": {},
                "properties": {"version": "12.0"},
            },
        ]
    )

    updated = discovery_node(
        _base_state(),
        DiscoveryRequest(use_live_azure=True, subscription_id="sub-001", resource_group="rg-a"),
        azure_resource_lister=mocked_lister,
    )

    mocked_lister.assert_called_once_with("sub-001", "rg-a")

    assert len(updated.discovered_resources) == 1
    discovered = updated.discovered_resources[0]

    assert discovered.resource_type == "Microsoft.KeyVault/vaults"
    assert discovered.name == "kv-prod"
    assert discovered.resource_group == "rg-a"
    assert discovered.region == "eastus"
    assert discovered.raw_properties["enableRbacAuthorization"] is True

    assert len(updated.manual_dependencies) == 1
    assert "Microsoft.Sql/servers" in updated.manual_dependencies[0]
    assert updated.audit_records[0].outputs["manual_dependency_count"] == 1
