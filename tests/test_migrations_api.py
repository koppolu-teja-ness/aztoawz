from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
import pytest

from src.api import create_app
from src.api.migrations import _get_graph_dependencies
from src.graph.build_graph import GraphDependencies

_BICEP_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bicep" / "functionapp_sample.bicep"


def _headers() -> dict[str, str]:
    return {
        "x-api-key": "capstone-api-key",
        "x-reviewer-id": "reviewer-1",
    }


class _NoopCloudFormationClient:
    pass


class _NoopSession:
    def client(self, _service_name: str, **_kwargs: Any) -> object:
        return object()


def _fake_validator_result() -> Any:
    return type("Result", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()


def _deterministic_dependencies() -> GraphDependencies:
    return GraphDependencies(
        mapping_retriever=lambda _query, _service, _k: [],
        validator_command_runner=lambda _command, _cwd: _fake_validator_result(),
        deployer_cloudformation_client=_NoopCloudFormationClient(),
        postvalidate_boto3_session=_NoopSession(),
    )


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MIGRATION_RUN_DIR", str(tmp_path / "runs"))
    app = create_app()
    app.dependency_overrides[_get_graph_dependencies] = _deterministic_dependencies
    return TestClient(app)


def test_migrations_api_happy_path(client: TestClient) -> None:
    create_response = client.post(
        "/migrations",
        json={"source_reference": "fixture://happy-path", "bicep_path": str(_BICEP_FIXTURE)},
        headers=_headers(),
    )
    assert create_response.status_code == 201

    created = create_response.json()
    migration_id = created["migration_id"]
    plan_hash = created["plan_hash"]

    status_response = client.get(f"/migrations/{migration_id}/status", headers=_headers())
    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["status"] == "AWAITING_APPROVAL"
    assert status_payload["has_approval_record"] is False

    plan_response = client.get(f"/migrations/{migration_id}/plan", headers=_headers())
    assert plan_response.status_code == 200
    plan_payload = plan_response.json()
    assert plan_payload["plan_hash"] == plan_hash
    assert plan_payload["items"]
    # Real discovery/parser output: both the storage account and function app were
    # discovered from the bicep fixture and appear in the real generated plan.
    resource_ids = {item["resource_id"] for item in plan_payload["items"]}
    assert any("functionApp" in rid or "sites" in rid for rid in resource_ids)

    reports_response = client.get(f"/migrations/{migration_id}/reports", headers=_headers())
    assert reports_response.status_code == 200
    reports_payload = reports_response.json()
    assert reports_payload["migration_id"] == migration_id
    assert reports_payload["report_paths"] == []
    assert reports_payload["has_approval_record"] is False

    approval_response = client.post(
        f"/migrations/{migration_id}/approval",
        json={
            "decision": "APPROVE",
            "plan_hash": plan_hash,
            "rationale": "approved for deployment",
        },
        headers=_headers(),
    )
    assert approval_response.status_code == 200
    approval_payload = approval_response.json()
    assert approval_payload["status"] == "APPROVED"
    assert approval_payload["approval_record"]["reviewer"] == "reviewer-1"

    final_status_response = client.get(f"/migrations/{migration_id}/status", headers=_headers())
    assert final_status_response.status_code == 200
    assert final_status_response.json()["status"] == "APPROVED"

    final_reports_response = client.get(f"/migrations/{migration_id}/reports", headers=_headers())
    assert final_reports_response.status_code == 200
    assert final_reports_response.json()["report_paths"]


def test_migration_approval_rejects_stale_plan_hash(client: TestClient) -> None:
    create_response = client.post(
        "/migrations",
        json={"bicep_path": str(_BICEP_FIXTURE)},
        headers=_headers(),
    )
    assert create_response.status_code == 201

    migration_id = create_response.json()["migration_id"]

    stale_response = client.post(
        f"/migrations/{migration_id}/approval",
        json={
            "decision": "APPROVE",
            "plan_hash": "sha256:stale-hash",
            "rationale": "should fail",
        },
        headers=_headers(),
    )

    assert stale_response.status_code == 409
    assert "plan_hash does not match" in stale_response.json()["detail"]


def test_migration_start_requires_bicep_path(client: TestClient) -> None:
    response = client.post("/migrations", json={}, headers=_headers())
    assert response.status_code == 422

