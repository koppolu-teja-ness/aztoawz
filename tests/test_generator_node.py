from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import yaml

from src.agents.generator import GeneratorRequest, generator_node
from src.graph.state import MappingResult, MigrationState, ParsedResource


def _load_parsed_fixture(name: str) -> list[ParsedResource]:
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "mapping" / name
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    return [ParsedResource.model_validate(item) for item in payload]


def _base_state(
    resources: list[ParsedResource],
    mapping_results: list[MappingResult],
) -> MigrationState:
    now = datetime(2026, 9, 29, 12, 30, tzinfo=timezone.utc)
    return MigrationState(
        run_id="run-generator-001",
        started_at=now,
        updated_at=now,
        parsed_resources=resources,
        mapping_results=mapping_results,
    )


def _assert_required_sections(template: dict[str, object]) -> None:
    assert "AWSTemplateFormatVersion" in template
    assert "Parameters" in template
    assert "Mappings" in template
    assert "Conditions" in template
    assert "Resources" in template
    assert "Outputs" in template


def test_generator_node_creates_one_stack_per_logical_unit_for_fully_mappable_fixture() -> None:
    resources = _load_parsed_fixture("fully_mappable.json")
    mapping_results = [
        MappingResult(
            source_resource_id="Microsoft.KeyVault/vaults/kv-demo",
            target_resource_type="AWS::SecretsManager::Secret + AWS::KMS::Key",
            mapped=True,
            rule_id="KV-001",
            confidence=0.96,
        ),
        MappingResult(
            source_resource_id="Microsoft.Web/sites/func-demo",
            target_resource_type="AWS::Lambda::Function",
            mapped=True,
            rule_id="FN-001",
            confidence=0.93,
        ),
        MappingResult(
            source_resource_id="Microsoft.Network/virtualNetworks/subnets/vnet-demo/subnet-app",
            target_resource_type="AWS::EC2::Subnet",
            mapped=True,
            rule_id="VNET-002",
            confidence=0.91,
        ),
    ]

    updated = generator_node(
        _base_state(resources, mapping_results),
        GeneratorRequest(),
        now_provider=lambda: datetime(2026, 9, 29, 12, 31, tzinfo=timezone.utc),
    )

    assert len(updated.cfn_artifacts) == 3
    assert {artifact.logical_id for artifact in updated.cfn_artifacts} == {
        "KeyVaultStack",
        "FunctionsStack",
        "NetworkingStack",
    }

    templates: dict[str, dict[str, object]] = {}
    for artifact in updated.cfn_artifacts:
        assert artifact.resource_type == "AWS::CloudFormation::Stack"
        template = yaml.safe_load(artifact.properties["template_yaml"])
        templates[artifact.logical_id] = template
        _assert_required_sections(template)

    functions_template = templates["FunctionsStack"]
    networking_template = templates["NetworkingStack"]

    function_resources = functions_template["Resources"]
    lambda_resource = function_resources["MigratedFunction1"]
    assert lambda_resource["Properties"]["VpcConfig"]["SubnetIds"][0]["Fn::ImportValue"] == {
        "Fn::Sub": "mig-networking-private-subnet-id"
    }

    networking_outputs = networking_template["Outputs"]
    assert networking_outputs["VpcId"]["Export"]["Name"] == {
        "Fn::Sub": "mig-networking-vpc-id"
    }
    assert networking_outputs["PrivateSubnetId"]["Export"]["Name"] == {
        "Fn::Sub": "mig-networking-private-subnet-id"
    }

    assert len(updated.audit_records) == 1
    audit = updated.audit_records[0]
    assert audit.outputs["stage"] == "generator"
    assert audit.outputs["stack_count"] == 3
    assert set(audit.rule_ids_used) == {"KV-001", "FN-001", "VNET-002"}


def test_generator_node_ignores_unmapped_results_and_keeps_required_sections() -> None:
    resources = _load_parsed_fixture("partially_mappable.json")
    mapping_results = [
        MappingResult(
            source_resource_id="Microsoft.KeyVault/vaults/kv-demo",
            target_resource_type="AWS::SecretsManager::Secret + AWS::KMS::Key",
            mapped=True,
            rule_id="KV-001",
            confidence=0.94,
        ),
        MappingResult(
            source_resource_id="Microsoft.Network/virtualNetworks/vnet-demo",
            target_resource_type="UNMAPPED",
            mapped=False,
            rule_id="UNMAPPED",
            confidence=0.0,
            caveats=["requires manual intervention"],
        ),
    ]

    updated = generator_node(
        _base_state(resources, mapping_results),
        GeneratorRequest(),
        now_provider=lambda: datetime(2026, 9, 29, 12, 32, tzinfo=timezone.utc),
    )

    assert len(updated.cfn_artifacts) == 1
    artifact = updated.cfn_artifacts[0]
    assert artifact.logical_id == "KeyVaultStack"

    template = yaml.safe_load(artifact.properties["template_yaml"])
    _assert_required_sections(template)

    resources_section = template["Resources"]
    assert "KmsKey1" in resources_section
    assert "MigratedSecret1" in resources_section

    assert len(updated.audit_records) == 1
    assert updated.audit_records[0].outputs["stack_count"] == 1
    assert updated.audit_records[0].rule_ids_used == ["KV-001"]


def test_generator_node_emits_audit_when_everything_is_unmapped() -> None:
    resources = _load_parsed_fixture("unsupported_expressroute.json")
    mapping_results = [
        MappingResult(
            source_resource_id="Microsoft.Network/expressRouteCircuits/er-demo",
            target_resource_type="UNMAPPED",
            mapped=False,
            rule_id="UNMAPPED",
            confidence=0.0,
            caveats=["out of scope"],
        )
    ]

    updated = generator_node(
        _base_state(resources, mapping_results),
        GeneratorRequest(),
        now_provider=lambda: datetime(2026, 9, 29, 12, 33, tzinfo=timezone.utc),
    )

    assert updated.cfn_artifacts == []
    assert len(updated.audit_records) == 1
    assert updated.audit_records[0].outputs["stage"] == "generator"
    assert updated.audit_records[0].outputs["stack_count"] == 0
    assert updated.audit_records[0].rule_ids_used == []
