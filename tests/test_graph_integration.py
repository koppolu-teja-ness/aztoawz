from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from src.agents.approval import ApprovalGateRequest, FileApprovalStore
from src.agents.discovery import DiscoveryRequest
from src.agents.parser import ParserRequest
from src.agents.reporter import ReporterRequest
from src.graph.build_graph import GraphDependencies, TraceSink, build_migration_graph
from src.graph.state import ApprovalDecision, ApprovalRecord, MigrationState


class _TraceCollector(TraceSink):
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)


class _NoopCloudFormationClient:
    pass


class _NoopSession:
    def client(self, _service_name: str, **_kwargs: Any) -> object:
        return object()


_FIXTURE_ARM_TEMPLATE: dict[str, Any] = {
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
    "contentVersion": "1.0.0.0",
    "parameters": {
        "functionAppName": {"type": "string", "defaultValue": "func-demo"},
        "storageAccountName": {"type": "string", "defaultValue": "stfuncdemo"},
        "location": {"type": "string", "defaultValue": "eastus"},
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
                            "name": "AzureWebJobsStorage",
                            "value": "UseDevelopmentStorage=true",
                        }
                    ]
                }
            },
        },
    ],
}


def _fake_parser_runner(command: list[str]) -> None:
    assert command[:3] == ["az", "bicep", "build"]
    outfile_index = command.index("--outfile") + 1
    output_file = Path(command[outfile_index])
    output_file.write_text(json.dumps(_FIXTURE_ARM_TEMPLATE), encoding="utf-8")


def test_full_graph_interrupt_resume_to_report_with_audit_records(tmp_path: Path) -> None:
    now = datetime(2026, 9, 29, 19, 0, tzinfo=timezone.utc)
    run_id = "run-graph-integration-001"

    bicep_fixture = Path(__file__).resolve().parent / "fixtures" / "bicep" / "functionapp_sample.bicep"
    approval_dir = tmp_path / "approval"
    reports_dir = tmp_path / "reports"

    approval_store = FileApprovalStore(approval_dir)
    trace_collector = _TraceCollector()

    graph = build_migration_graph(
        discovery_request=DiscoveryRequest(bicep_path=str(bicep_fixture)),
        parser_request=ParserRequest(
            bicep_path=str(bicep_fixture),
            artifacts_dir=str(tmp_path / "artifacts"),
            parameters={"dbPassword": "not-a-real-secret"},
        ),
        approval_request=ApprovalGateRequest(checkpoint_dir=str(approval_dir)),
        reporter_request=ReporterRequest(output_dir=str(reports_dir)),
        checkpointer=MemorySaver(),
        trace_backend="custom",
        trace_sinks=[trace_collector],
        dependencies=GraphDependencies(
            parser_command_runner=_fake_parser_runner,
            mapping_retriever=lambda _query, _service, _k: [],
            validator_command_runner=lambda _command, _cwd: type(
                "Result", (), {"returncode": 0, "stdout": "[]", "stderr": ""}
            )(),
            approval_store=approval_store,
            deployer_cloudformation_client=_NoopCloudFormationClient(),
            postvalidate_boto3_session=_NoopSession(),
        ),
    )

    thread = {"configurable": {"thread_id": "graph-integration-thread"}}

    interrupted = graph.invoke(
        {
            "migration_state": MigrationState(
                run_id=run_id,
                started_at=now,
                updated_at=now,
            )
        },
        config=thread,
    )

    assert "__interrupt__" in interrupted

    pending = approval_store.get_pending_plan()
    assert pending is not None
    assert pending.plan_hash

    approval_store.save_approval_record(
        ApprovalRecord(
            decision=ApprovalDecision.APPROVE,
            reviewer="integration-reviewer",
            timestamp=now,
            plan_hash=pending.plan_hash,
            comments="approved by integration test",
        )
    )

    resumed = graph.invoke(Command(resume={"approved": True}), config=thread)
    final_state = resumed["migration_state"]

    stages = [record.outputs.get("stage") for record in final_state.audit_records]
    assert stages == [
        "discovery",
        "parser",
        "mapping",
        "generator",
        "validator",
        "planner",
        "approval_gate",
        "deployer",
        "postvalidate",
        "reporter",
    ]

    assert final_state.deployment_result is not None
    assert final_state.deployment_result.deployed is True

    report_dir = reports_dir / run_id
    assert (report_dir / "migration-plan.md").exists()
    assert (report_dir / "risk-report.md").exists()
    assert (report_dir / "execution-report.md").exists()
    assert (report_dir / "validation-report.md").exists()

    trace_dump = json.dumps(trace_collector.events)
    assert "not-a-real-secret" not in trace_dump
    assert "***REDACTED***" in trace_dump
