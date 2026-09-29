from __future__ import annotations

from datetime import datetime, timezone

from src.agents.planner import PlannerRequest, planner_node
from src.graph.state import (
    CfnArtifact,
    MappingResult,
    MigrationState,
    ParsedResource,
    PlanItemStatus,
    ResourceDependencyGraph,
    ResourceGraphEdge,
    ResourceGraphNode,
)


def _base_state() -> MigrationState:
    now = datetime(2026, 9, 29, 13, 0, tzinfo=timezone.utc)

    resources = [
        ParsedResource(
            resource_id="Microsoft.Network/virtualNetworks/vnet-app",
            source_type="Microsoft.Network/virtualNetworks",
            normalized_type="microsoft_network_virtualnetworks",
            properties={"resolved_properties": {"addressSpace": {"addressPrefixes": ["10.0.0.0/16"]}}},
            dependencies=[],
        ),
        ParsedResource(
            resource_id="Microsoft.KeyVault/vaults/kv-app",
            source_type="Microsoft.KeyVault/vaults",
            normalized_type="microsoft_keyvault_vaults",
            properties={"resolved_properties": {"enableRbacAuthorization": True}},
            dependencies=[],
        ),
        ParsedResource(
            resource_id="Microsoft.Web/sites/func-app",
            source_type="Microsoft.Web/sites",
            normalized_type="microsoft_web_sites",
            properties={"resolved_properties": {"httpsOnly": True}},
            dependencies=["Microsoft.Network/virtualNetworks/vnet-app"],
        ),
        ParsedResource(
            resource_id="Contoso.App/configurations/app-config",
            source_type="Contoso.App/configurations",
            normalized_type="contoso_app_configurations",
            properties={
                "resolved_properties": {
                    "secretUri": "https://kv-app.vault.azure.net/secrets/db-password"
                }
            },
            dependencies=[
                "Microsoft.Web/sites/func-app",
                "Microsoft.KeyVault/vaults/kv-app",
            ],
        ),
    ]

    graph = ResourceDependencyGraph(
        nodes=[
            ResourceGraphNode(
                resource_id=item.resource_id,
                source_type=item.source_type,
                name=item.resource_id.split("/")[-1],
                scope="resourceGroup",
                origin_ref="fixture://planner",
            )
            for item in resources
        ],
        edges=[
            ResourceGraphEdge(
                from_resource_id="Microsoft.Network/virtualNetworks/vnet-app",
                to_resource_id="Microsoft.Web/sites/func-app",
                reason="dependsOn",
            ),
            ResourceGraphEdge(
                from_resource_id="Microsoft.Web/sites/func-app",
                to_resource_id="Contoso.App/configurations/app-config",
                reason="dependsOn",
            ),
            ResourceGraphEdge(
                from_resource_id="Microsoft.KeyVault/vaults/kv-app",
                to_resource_id="Contoso.App/configurations/app-config",
                reason="dependsOn",
            ),
        ],
    )

    mapping_results = [
        MappingResult(
            source_resource_id="Microsoft.Network/virtualNetworks/vnet-app",
            target_resource_type="AWS::EC2::VPC",
            mapped=True,
            rule_id="VNET-001",
            confidence=0.92,
        ),
        MappingResult(
            source_resource_id="Microsoft.KeyVault/vaults/kv-app",
            target_resource_type="AWS::SecretsManager::Secret + AWS::KMS::Key",
            mapped=True,
            rule_id="KV-001",
            confidence=0.97,
        ),
        MappingResult(
            source_resource_id="Microsoft.Web/sites/func-app",
            target_resource_type="AWS::Lambda::Function",
            mapped=True,
            rule_id="FN-001",
            confidence=0.95,
        ),
        MappingResult(
            source_resource_id="Contoso.App/configurations/app-config",
            target_resource_type="UNMAPPED",
            mapped=False,
            rule_id="UNMAPPED",
            confidence=0.0,
            caveats=["requires manual intervention"],
        ),
    ]

    networking_template = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  PublicIngress:
    Type: AWS::EC2::SecurityGroupIngress
    Properties:
      CidrIp: 0.0.0.0/0
""".strip()

    function_template = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  FunctionExecutionRole:
    Type: AWS::IAM::Role
    Properties:
      Policies:
        - PolicyName: wildcard
          PolicyDocument:
            Statement:
              - Effect: Allow
                Action: '*'
                Resource: '*'
""".strip()

    keyvault_template = """
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  MigratedSecret:
    Type: AWS::SecretsManager::Secret
    Properties:
      Name: kv-app
""".strip()

    artifacts = [
        CfnArtifact(
            logical_id="NetworkingStack",
            resource_type="AWS::CloudFormation::Stack",
            properties={"unit": "networking", "template_yaml": networking_template},
            rule_ids_used=["VNET-001"],
        ),
        CfnArtifact(
            logical_id="FunctionsStack",
            resource_type="AWS::CloudFormation::Stack",
            properties={"unit": "functions", "template_yaml": function_template},
            rule_ids_used=["FN-001"],
        ),
        CfnArtifact(
            logical_id="KeyVaultStack",
            resource_type="AWS::CloudFormation::Stack",
            properties={"unit": "keyvault", "template_yaml": keyvault_template},
            rule_ids_used=["KV-001"],
        ),
    ]

    return MigrationState(
        run_id="run-planner-001",
        started_at=now,
        updated_at=now,
        parsed_resources=resources,
        parsed_dependency_graph=graph,
        mapping_results=mapping_results,
        cfn_artifacts=artifacts,
        manual_dependencies=[
            "UNMAPPED requires manual intervention: Contoso.App/configurations/app-config"
        ],
    )


def test_planner_node_builds_deployment_order_and_risk_labels() -> None:
    updated = planner_node(
        _base_state(),
        PlannerRequest(),
        now_provider=lambda: datetime(2026, 9, 29, 13, 1, tzinfo=timezone.utc),
    )

    assert [item.resource_id for item in updated.migration_plan] == [
        "Microsoft.Network/virtualNetworks/vnet-app",
        "Microsoft.KeyVault/vaults/kv-app",
        "Microsoft.Web/sites/func-app",
        "Contoso.App/configurations/app-config",
    ]

    by_resource = {item.resource_id: item for item in updated.migration_plan}
    assert by_resource["Microsoft.Network/virtualNetworks/vnet-app"].status == PlanItemStatus.HIGH_RISK
    assert by_resource["Microsoft.Web/sites/func-app"].status == PlanItemStatus.HIGH_RISK
    assert by_resource["Microsoft.KeyVault/vaults/kv-app"].status == PlanItemStatus.AUTO
    assert by_resource["Contoso.App/configurations/app-config"].status == PlanItemStatus.HIGH_RISK

    assert updated.plan_hash is not None
    assert updated.plan_hash.startswith("sha256:")
    assert len(updated.risk_scores) == 4


def test_planner_node_plan_hash_is_deterministic_for_same_input() -> None:
    state = _base_state()

    first = planner_node(
        state,
        PlannerRequest(),
        now_provider=lambda: datetime(2026, 9, 29, 13, 2, tzinfo=timezone.utc),
    )
    second = planner_node(
        state,
        PlannerRequest(),
        now_provider=lambda: datetime(2026, 9, 29, 13, 3, tzinfo=timezone.utc),
    )

    assert first.plan_hash == second.plan_hash


def test_planner_node_renders_markdown_and_html_summary_tables() -> None:
    updated = planner_node(
        _base_state(),
        PlannerRequest(),
        now_provider=lambda: datetime(2026, 9, 29, 13, 4, tzinfo=timezone.utc),
    )

    assert updated.migration_plan_report is not None

    report = updated.migration_plan_report
    assert report.total_items == 4
    assert report.auto_migratable_count == 1
    assert report.requires_review_count == 0
    assert report.high_risk_count == 3

    assert "| Category | Count |" in report.markdown_summary
    assert "| Stage | Resource | Sequence | Dependency | Owner |" in report.markdown_summary
    assert "| Resource | Risk Level | Rationale | Required Action |" in report.markdown_summary
    assert "<table>" in report.html_summary
    assert "<th>Category</th><th>Count</th>" in report.html_summary

    assert updated.audit_records[-1].outputs["stage"] == "planner"
