from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from src.agents.approval import (
    ApprovalGateRequest,
    FileApprovalStore,
    PendingPlanCheckpoint,
    approval_allows_deployment,
    approval_gate_node,
)
from src.api import create_app
from src.graph.state import ApprovalDecision, MigrationPlanItem, MigrationState, PlanItemStatus


class ApprovalFlowState(TypedDict):
    migration_state: MigrationState
    deployed: bool


def _base_state(plan_hash: str = "sha256:plan-001") -> MigrationState:
    now = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
    return MigrationState(
        run_id="run-approval-001",
        started_at=now,
        updated_at=now,
        migration_plan=[
            MigrationPlanItem(
                resource_id="Microsoft.Web/sites/func-app",
                action="Deploy mapped construct: AWS::Lambda::Function",
                status=PlanItemStatus.REVIEW,
                risk_score=35,
                notes=[],
            )
        ],
        plan_hash=plan_hash,
    )


def _build_graph(checkpoint_dir: Path):
    store = FileApprovalStore(checkpoint_dir)

    def gate(state: ApprovalFlowState) -> dict[str, object]:
        updated = approval_gate_node(
            state["migration_state"],
            ApprovalGateRequest(checkpoint_dir=str(checkpoint_dir)),
            store=store,
            now_provider=lambda: datetime(2026, 9, 29, 14, 1, tzinfo=timezone.utc),
        )
        return {"migration_state": updated}

    def route(state: ApprovalFlowState) -> str:
        return "deploy" if approval_allows_deployment(state["migration_state"]) else "blocked"

    def deploy(_: ApprovalFlowState) -> dict[str, bool]:
        return {"deployed": True}

    def blocked(_: ApprovalFlowState) -> dict[str, bool]:
        return {"deployed": False}

    graph = StateGraph(ApprovalFlowState)
    graph.add_node("approval_gate", gate)
    graph.add_node("deploy", deploy)
    graph.add_node("blocked", blocked)
    graph.add_edge(START, "approval_gate")
    graph.add_conditional_edges(
        "approval_gate",
        route,
        {"deploy": "deploy", "blocked": "blocked"},
    )
    graph.add_edge("deploy", END)
    graph.add_edge("blocked", END)
    return graph.compile(checkpointer=MemorySaver())


def test_submit_decision_rejects_stale_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkpoint_dir = tmp_path / "checkpoints"
    monkeypatch.setenv("APPROVAL_CHECKPOINT_DIR", str(checkpoint_dir))

    store = FileApprovalStore(checkpoint_dir)
    store.save_pending_plan(
        PendingPlanCheckpoint(
            run_id="run-approval-001",
            plan_hash="sha256:current",
            migration_plan=_base_state().migration_plan,
            created_at=datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc),
        )
    )

    client = TestClient(create_app())
    response = client.post(
        "/approval/decision",
        json={
            "decision": "APPROVE",
            "plan_hash": "sha256:stale",
            "reviewer": "reviewer-1",
            "comments": "stale hash attempt",
        },
    )

    assert response.status_code == 409
    assert "plan_hash does not match" in response.json()["detail"]


@pytest.mark.parametrize(
    ("decision", "expected_deployed"),
    [
        (ApprovalDecision.APPROVE, True),
        (ApprovalDecision.REJECT, False),
        (ApprovalDecision.MODIFY, False),
    ],
)
def test_approval_decision_controls_graph_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: ApprovalDecision,
    expected_deployed: bool,
) -> None:
    checkpoint_dir = tmp_path / "checkpoints"
    monkeypatch.setenv("APPROVAL_CHECKPOINT_DIR", str(checkpoint_dir))

    graph = _build_graph(checkpoint_dir)
    thread = {"configurable": {"thread_id": f"thread-{decision.value.lower()}"}}

    interrupted = graph.invoke(
        {
            "migration_state": _base_state(),
            "deployed": False,
        },
        config=thread,
    )

    assert "__interrupt__" in interrupted

    client = TestClient(create_app())
    response = client.post(
        "/approval/decision",
        json={
            "decision": decision.value,
            "plan_hash": "sha256:plan-001",
            "reviewer": "reviewer-1",
            "comments": "review complete",
        },
    )
    assert response.status_code == 200

    resumed = graph.invoke(Command(resume={"ack": True}), config=thread)
    assert resumed["deployed"] is expected_deployed
