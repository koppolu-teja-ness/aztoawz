from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from src.agents.mapping import MappingRequest, mapping_node
from src.graph.state import MigrationState, ParsedResource
from src.kb.retriever import MappingCandidate


def _load_fixture(name: str) -> list[ParsedResource]:
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "mapping" / name
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    return [ParsedResource.model_validate(item) for item in payload]


def _base_state(resources: list[ParsedResource]) -> MigrationState:
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    return MigrationState(
        run_id="run-mapping-001",
        started_at=now,
        updated_at=now,
        parsed_resources=resources,
    )


def test_mapping_node_fully_mappable_fixture() -> None:
    resources = _load_fixture("fully_mappable.json")
    calls: list[tuple[str, str, int]] = []

    def fake_retriever(query: str, source_service: str, k: int) -> list[MappingCandidate]:
        calls.append((query, source_service, k))
        if source_service == "Azure Key Vault":
            return [
                MappingCandidate(
                    rule_id="KV-001",
                    mapped_construct="AWS::SecretsManager::Secret + AWS::KMS::Key",
                    confidence=0.96,
                    source_service=source_service,
                    target_service="AWS Secrets Stack",
                )
            ]
        if source_service == "Azure Functions":
            return [
                MappingCandidate(
                    rule_id="FN-001",
                    mapped_construct="AWS::Lambda::Function",
                    confidence=0.93,
                    source_service=source_service,
                    target_service="AWS Lambda",
                )
            ]
        return [
            MappingCandidate(
                rule_id="VNET-002",
                mapped_construct="AWS::EC2::Subnet",
                confidence=0.91,
                source_service=source_service,
                target_service="AWS VPC",
            )
        ]

    updated = mapping_node(
        _base_state(resources),
        MappingRequest(),
        retriever=fake_retriever,
        now_provider=lambda: datetime(2026, 9, 29, 12, 1, tzinfo=timezone.utc),
    )

    assert len(calls) == len(resources)
    assert all(result.mapped for result in updated.mapping_results)
    assert {result.rule_id for result in updated.mapping_results} == {
        "KV-001",
        "FN-001",
        "VNET-002",
    }

    assert len(updated.audit_records) == 1
    audit = updated.audit_records[0]
    assert audit.outputs["stage"] == "mapping"
    assert audit.outputs["mapped_count"] == 3
    assert audit.outputs["unmapped_count"] == 0
    assert set(audit.rule_ids_used) == {"KV-001", "FN-001", "VNET-002"}


def test_mapping_node_partially_mappable_fixture_uses_unmapped_threshold() -> None:
    resources = _load_fixture("partially_mappable.json")
    calls: list[tuple[str, str, int]] = []

    def fake_retriever(query: str, source_service: str, k: int) -> list[MappingCandidate]:
        calls.append((query, source_service, k))
        if source_service == "Azure Key Vault":
            return [
                MappingCandidate(
                    rule_id="KV-001",
                    mapped_construct="AWS::SecretsManager::Secret + AWS::KMS::Key",
                    confidence=0.90,
                    source_service=source_service,
                    target_service="AWS Secrets Stack",
                )
            ]

        return [
            MappingCandidate(
                rule_id="VNET-001",
                mapped_construct="AWS::EC2::VPC",
                confidence=0.42,
                source_service=source_service,
                target_service="AWS VPC",
            )
        ]

    updated = mapping_node(
        _base_state(resources),
        MappingRequest(),
        retriever=fake_retriever,
        now_provider=lambda: datetime(2026, 9, 29, 12, 2, tzinfo=timezone.utc),
    )

    assert len(calls) == len(resources)
    assert len(updated.mapping_results) == 2

    mapped = [result for result in updated.mapping_results if result.mapped]
    unmapped = [result for result in updated.mapping_results if not result.mapped]

    assert len(mapped) == 1
    assert mapped[0].rule_id == "KV-001"
    assert len(unmapped) == 1
    assert unmapped[0].rule_id == "UNMAPPED"
    assert unmapped[0].target_resource_type == "UNMAPPED"
    assert any("requires manual intervention" in caveat for caveat in unmapped[0].caveats)

    assert any("Microsoft.Network/virtualNetworks/vnet-demo" in item for item in updated.manual_dependencies)
    assert updated.audit_records[0].outputs["mapped_count"] == 1
    assert updated.audit_records[0].outputs["unmapped_count"] == 1
    assert updated.audit_records[0].rule_ids_used == ["KV-001"]


def test_mapping_node_marks_expressroute_as_unmapped() -> None:
    resources = _load_fixture("unsupported_expressroute.json")
    calls: list[tuple[str, str, int]] = []

    def fake_retriever(query: str, source_service: str, k: int) -> list[MappingCandidate]:
        calls.append((query, source_service, k))
        return [
            MappingCandidate(
                rule_id="VNET-004",
                mapped_construct="AWS::EC2::RouteTable + AWS::EC2::Route",
                confidence=0.99,
                source_service=source_service,
                target_service="AWS VPC",
            )
        ]

    updated = mapping_node(
        _base_state(resources),
        MappingRequest(),
        retriever=fake_retriever,
        now_provider=lambda: datetime(2026, 9, 29, 12, 3, tzinfo=timezone.utc),
    )

    assert len(calls) == 1
    assert calls[0][1] == "Azure Virtual Network"

    assert len(updated.mapping_results) == 1
    result = updated.mapping_results[0]
    assert result.mapped is False
    assert result.rule_id == "UNMAPPED"
    assert result.target_resource_type == "UNMAPPED"
    assert any("ExpressRoute" in caveat for caveat in result.caveats)

    assert updated.audit_records[0].outputs["mapped_count"] == 0
    assert updated.audit_records[0].outputs["unmapped_count"] == 1
    assert updated.audit_records[0].rule_ids_used == []
