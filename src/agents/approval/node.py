"""Human approval gate node with durable checkpointing and interrupt/resume flow."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable

from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field

from src.graph.state import ApprovalDecision, ApprovalRecord, AuditRecord, MigrationPlanItem, MigrationState


class ApprovalGateRequest(BaseModel):
    """Input contract for human-gate stage execution."""

    model_config = ConfigDict(extra="forbid")

    checkpoint_dir: str = "checkpoints"


class PendingPlanCheckpoint(BaseModel):
    """Persisted approval payload required to resume a run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    plan_hash: str
    migration_plan: list[MigrationPlanItem] = Field(default_factory=list)
    created_at: datetime


NowCallable = Callable[[], datetime]


class FileApprovalStore:
    """Durable approval checkpoint store backed by JSON files."""

    def __init__(self, checkpoint_dir: str | Path = "checkpoints") -> None:
        self.checkpoint_dir = Path(checkpoint_dir)
        self.pending_path = self.checkpoint_dir / "pending_plan.json"
        self.approval_path = self.checkpoint_dir / "approval_record.json"

    def get_pending_plan(self) -> PendingPlanCheckpoint | None:
        if not self.pending_path.exists():
            return None
        payload = json.loads(self.pending_path.read_text(encoding="utf-8"))
        return PendingPlanCheckpoint.model_validate(payload)

    def save_pending_plan(self, pending: PendingPlanCheckpoint) -> PendingPlanCheckpoint:
        self._atomic_write_json(self.pending_path, pending.model_dump(mode="json"))
        return pending

    def get_approval_record(self) -> ApprovalRecord | None:
        if not self.approval_path.exists():
            return None
        payload = json.loads(self.approval_path.read_text(encoding="utf-8"))
        return ApprovalRecord.model_validate(payload)

    def save_approval_record(self, approval: ApprovalRecord) -> ApprovalRecord:
        self._atomic_write_json(self.approval_path, approval.model_dump(mode="json"))
        return approval

    def _atomic_write_json(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(path)


def approval_gate_node(
    state: MigrationState,
    request: ApprovalGateRequest,
    *,
    store: FileApprovalStore | None = None,
    now_provider: NowCallable | None = None,
) -> MigrationState:
    """Pause execution for human decision, then resume only with persisted matching approval."""

    if state.plan_hash is None:
        raise ValueError("Approval gate requires state.plan_hash from planner stage")

    now_fn = now_provider or (lambda: datetime.now(tz=timezone.utc))
    approval_store = store or FileApprovalStore(request.checkpoint_dir)

    pending = PendingPlanCheckpoint(
        run_id=state.run_id,
        plan_hash=state.plan_hash,
        migration_plan=state.migration_plan,
        created_at=now_fn(),
    )
    approval_store.save_pending_plan(pending)

    interrupt(
        {
            "event": "approval_required",
            "run_id": state.run_id,
            "plan_hash": state.plan_hash,
            "pending_plan_path": str(approval_store.pending_path),
            "approval_record_path": str(approval_store.approval_path),
        }
    )

    approval = approval_store.get_approval_record()
    if approval is None:
        raise RuntimeError("Graph resumed without a persisted approval record")
    if approval.plan_hash != state.plan_hash:
        raise RuntimeError("Persisted approval plan_hash does not match current pending plan")

    timestamp = now_fn()
    audit = AuditRecord(
        inputs_hash=state.plan_hash,
        outputs={
            "stage": "approval_gate",
            "decision": approval.decision.value,
            "plan_hash": approval.plan_hash,
        },
        rule_ids_used=[],
        reviewer=approval.reviewer,
        timestamp=timestamp,
    )

    return state.model_copy(
        update={
            "updated_at": timestamp,
            "approval_record": approval,
            "audit_records": [*state.audit_records, audit],
        }
    )


def approval_allows_deployment(state: MigrationState) -> bool:
    """Return True only when a matching APPROVE decision exists."""

    return (
        state.approval_record is not None
        and state.plan_hash is not None
        and state.approval_record.plan_hash == state.plan_hash
        and state.approval_record.decision == ApprovalDecision.APPROVE
    )
