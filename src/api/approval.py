"""Approval gate FastAPI endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
import os

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from src.agents.approval import FileApprovalStore, PendingPlanCheckpoint
from src.graph.state import ApprovalDecision, ApprovalRecord


router = APIRouter(prefix="/approval", tags=["approval"])


class ApprovalDecisionRequest(BaseModel):
    """Payload for approval decisions on a pending migration plan."""

    model_config = ConfigDict(extra="forbid")

    decision: ApprovalDecision
    plan_hash: str = Field(min_length=1)
    reviewer: str = Field(min_length=1)
    comments: str | None = None


def _get_store() -> FileApprovalStore:
    checkpoint_dir = os.getenv("APPROVAL_CHECKPOINT_DIR", "checkpoints")
    return FileApprovalStore(checkpoint_dir)


@router.get("/pending", response_model=PendingPlanCheckpoint)
def get_pending_plan(store: FileApprovalStore = Depends(_get_store)) -> PendingPlanCheckpoint:
    """Fetch the currently pending approval plan checkpoint."""

    pending = store.get_pending_plan()
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No pending approval plan found",
        )
    return pending


@router.post("/decision", response_model=ApprovalRecord)
def submit_approval_decision(
    request: ApprovalDecisionRequest,
    store: FileApprovalStore = Depends(_get_store),
) -> ApprovalRecord:
    """Record an approval decision for the currently pending plan hash."""

    pending = store.get_pending_plan()
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No pending approval plan found",
        )

    if request.plan_hash != pending.plan_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="plan_hash does not match current pending plan",
        )

    record = ApprovalRecord(
        decision=request.decision,
        reviewer=request.reviewer,
        timestamp=datetime.now(tz=timezone.utc),
        plan_hash=request.plan_hash,
        comments=request.comments,
    )
    return store.save_approval_record(record)