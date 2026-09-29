"""FastAPI endpoints for migration run lifecycle management."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
from typing import Any
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from langgraph.types import Command

from src.agents.approval import ApprovalGateRequest, FileApprovalStore
from src.agents.discovery import DiscoveryRequest
from src.agents.parser import ParserRequest
from src.agents.reporter import ReporterRequest
from src.api.auth import ApiPrincipal, require_auth, require_scope
from src.api.schemas import (
    MigrationApprovalRequest,
    MigrationApprovalResponse,
    MigrationPlanItemResponse,
    MigrationPlanResponse,
    MigrationReportsResponse,
    MigrationRunStatus,
    MigrationStartRequest,
    MigrationStartResponse,
    MigrationStatusCounts,
    MigrationStatusResponse,
)
from src.graph.build_graph import GraphDependencies, build_migration_graph
from src.graph.state import ApprovalDecision, ApprovalRecord, MigrationState


router = APIRouter(prefix="/migrations", tags=["migrations"])


@dataclass
class _RunEnvelope:
    migration_id: str
    status: MigrationRunStatus
    state: MigrationState
    created_at: datetime
    updated_at: datetime
    graph: Any
    thread: dict[str, Any]
    approval_store: FileApprovalStore


class _MigrationRunStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, _RunEnvelope] = {}

    def create(self, envelope: _RunEnvelope) -> _RunEnvelope:
        with self._lock:
            self._runs[envelope.migration_id] = envelope
        return envelope

    def get(self, migration_id: str) -> _RunEnvelope | None:
        with self._lock:
            return self._runs.get(migration_id)

    def save(self, envelope: _RunEnvelope) -> _RunEnvelope:
        with self._lock:
            self._runs[envelope.migration_id] = envelope
        return envelope


_STORE = _MigrationRunStore()


def _get_store() -> _MigrationRunStore:
    return _STORE


def _get_graph_dependencies() -> GraphDependencies | None:
    """Real production dependencies (None -> live az CLI / cfn-lint / checkov / boto3).

    Overridden in tests via FastAPI dependency_overrides to inject deterministic doubles.
    """

    return None


def _run_root(migration_id: str) -> Path:
    base = Path(os.getenv("MIGRATION_RUN_DIR", ".migration_runs"))
    return base / migration_id


@router.post("", response_model=MigrationStartResponse, status_code=status.HTTP_201_CREATED)
def start_migration_run(
    request: MigrationStartRequest,
    principal: ApiPrincipal = Depends(require_auth),
    store: _MigrationRunStore = Depends(_get_store),
    dependencies: GraphDependencies | None = Depends(_get_graph_dependencies),
) -> MigrationStartResponse:
    """Run discovery through planning on a real Bicep source and pause for human approval."""

    require_scope(principal, "migration:write")

    now = datetime.now(tz=timezone.utc)
    migration_id = f"mig-{uuid.uuid4().hex[:12]}"
    run_root = _run_root(migration_id)

    graph = build_migration_graph(
        discovery_request=DiscoveryRequest(
            bicep_path=request.bicep_path,
            use_live_azure=request.use_live_azure,
            subscription_id=request.subscription_id,
            resource_group=request.resource_group,
        ),
        parser_request=ParserRequest(
            bicep_path=request.bicep_path,
            artifacts_dir=str(run_root / "artifacts"),
        ),
        approval_request=ApprovalGateRequest(checkpoint_dir=str(run_root / "approval")),
        reporter_request=ReporterRequest(output_dir=str(run_root / "reports")),
        checkpoint_backend="sqlite",
        checkpoint_connection=str(run_root / "graph.sqlite"),
        dependencies=dependencies or GraphDependencies(),
    )

    thread = {"configurable": {"thread_id": migration_id}}
    result = graph.invoke(
        {"migration_state": MigrationState(run_id=migration_id, started_at=now, updated_at=now)},
        config=thread,
    )

    if "migration_state" not in result:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Migration graph did not produce state before approval interrupt",
        )

    state = result["migration_state"]

    if state.plan_hash is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to initialize migration plan",
        )

    envelope = _RunEnvelope(
        migration_id=migration_id,
        status=MigrationRunStatus.AWAITING_APPROVAL,
        state=state,
        created_at=now,
        updated_at=now,
        graph=graph,
        thread=thread,
        approval_store=FileApprovalStore(run_root / "approval"),
    )
    store.create(envelope)

    return MigrationStartResponse(
        migration_id=migration_id,
        status=envelope.status,
        plan_hash=state.plan_hash,
        created_at=now,
    )


@router.get("/{migration_id}/status", response_model=MigrationStatusResponse)
def get_migration_status(
    migration_id: str,
    principal: ApiPrincipal = Depends(require_auth),
    store: _MigrationRunStore = Depends(_get_store),
) -> MigrationStatusResponse:
    """Return current run status and planning summary."""

    require_scope(principal, "migration:read")
    envelope = _require_envelope(store, migration_id)

    if envelope.state.plan_hash is None or envelope.state.migration_plan_report is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Migration plan is unavailable")

    summary = _summary_from_state(envelope.state)
    return MigrationStatusResponse(
        migration_id=migration_id,
        status=envelope.status,
        plan_hash=envelope.state.plan_hash,
        created_at=envelope.created_at,
        updated_at=envelope.updated_at,
        summary=summary,
        has_approval_record=envelope.state.approval_record is not None,
    )


@router.get("/{migration_id}/plan", response_model=MigrationPlanResponse)
def get_migration_plan(
    migration_id: str,
    principal: ApiPrincipal = Depends(require_auth),
    store: _MigrationRunStore = Depends(_get_store),
) -> MigrationPlanResponse:
    """Return plan details and planner-rendered summaries for a run."""

    require_scope(principal, "migration:read")
    envelope = _require_envelope(store, migration_id)

    if envelope.state.plan_hash is None or envelope.state.migration_plan_report is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Migration plan is unavailable")

    report = envelope.state.migration_plan_report
    mapping_rule_by_resource = {
        item.source_resource_id: item.rule_id for item in envelope.state.mapping_results
    }
    return MigrationPlanResponse(
        migration_id=migration_id,
        plan_hash=envelope.state.plan_hash,
        summary=_summary_from_state(envelope.state),
        items=[
            MigrationPlanItemResponse(
                resource_id=item.resource_id,
                action=item.action,
                status=item.status,
                risk_score=item.risk_score,
                rule_id=mapping_rule_by_resource.get(item.resource_id),
                notes=item.notes,
            )
            for item in envelope.state.migration_plan
        ],
        markdown_summary=report.markdown_summary,
        html_summary=report.html_summary,
    )


@router.post("/{migration_id}/approval", response_model=MigrationApprovalResponse)
def submit_migration_approval(
    migration_id: str,
    request: MigrationApprovalRequest,
    principal: ApiPrincipal = Depends(require_auth),
    store: _MigrationRunStore = Depends(_get_store),
) -> MigrationApprovalResponse:
    """Persist an approval decision and resume the real graph through deploy/report."""

    require_scope(principal, "migration:approve")
    envelope = _require_envelope(store, migration_id)

    if envelope.state.plan_hash is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Migration plan is unavailable")

    pending = envelope.approval_store.get_pending_plan()
    if pending is None or pending.plan_hash != envelope.state.plan_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Migration run is not currently awaiting approval",
        )

    if request.plan_hash != envelope.state.plan_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="plan_hash does not match current migration plan",
        )

    now = datetime.now(tz=timezone.utc)
    approval_record = ApprovalRecord(
        decision=request.decision,
        reviewer=principal.subject,
        timestamp=now,
        plan_hash=request.plan_hash,
        comments=request.rationale,
    )
    envelope.approval_store.save_approval_record(approval_record)

    resumed = envelope.graph.invoke(
        Command(resume={"decision": request.decision.value}),
        config=envelope.thread,
    )

    if "migration_state" not in resumed:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Migration graph failed to resume after approval decision",
        )

    updated_state = resumed["migration_state"]
    next_status = _status_from_decision(request.decision)
    updated_envelope = _RunEnvelope(
        migration_id=envelope.migration_id,
        status=next_status,
        state=updated_state,
        created_at=envelope.created_at,
        updated_at=now,
        graph=envelope.graph,
        thread=envelope.thread,
        approval_store=envelope.approval_store,
    )
    store.save(updated_envelope)

    return MigrationApprovalResponse(
        migration_id=migration_id,
        status=next_status,
        approval_record=approval_record,
    )


@router.get("/{migration_id}/reports", response_model=MigrationReportsResponse)
def get_migration_reports(
    migration_id: str,
    principal: ApiPrincipal = Depends(require_auth),
    store: _MigrationRunStore = Depends(_get_store),
) -> MigrationReportsResponse:
    """Return generated report artifact paths for a migration run."""

    require_scope(principal, "migration:read")
    envelope = _require_envelope(store, migration_id)

    if envelope.state.plan_hash is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Migration plan is unavailable")

    report_paths = _latest_report_paths(envelope.state)
    return MigrationReportsResponse(
        migration_id=migration_id,
        plan_hash=envelope.state.plan_hash,
        report_paths=report_paths,
        has_approval_record=envelope.state.approval_record is not None,
        generated_at=envelope.updated_at,
    )


def _require_envelope(store: _MigrationRunStore, migration_id: str) -> _RunEnvelope:
    envelope = store.get(migration_id)
    if envelope is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Migration run not found")
    return envelope


def _summary_from_state(state: MigrationState) -> MigrationStatusCounts:
    report = state.migration_plan_report
    if report is None:
        return MigrationStatusCounts(
            total_items=0,
            auto_migratable_count=0,
            requires_review_count=0,
            high_risk_count=0,
        )

    return MigrationStatusCounts(
        total_items=report.total_items,
        auto_migratable_count=report.auto_migratable_count,
        requires_review_count=report.requires_review_count,
        high_risk_count=report.high_risk_count,
    )


def _status_from_decision(decision: ApprovalDecision) -> MigrationRunStatus:
    if decision == ApprovalDecision.APPROVE:
        return MigrationRunStatus.APPROVED
    if decision == ApprovalDecision.REJECT:
        return MigrationRunStatus.REJECTED
    return MigrationRunStatus.MODIFY_REQUESTED


def _latest_report_paths(state: MigrationState) -> list[str]:
    for audit in reversed(state.audit_records):
        stage = audit.outputs.get("stage")
        if stage != "reporter":
            continue
        generated = audit.outputs.get("generated_reports")
        if isinstance(generated, list):
            return [str(item) for item in generated if isinstance(item, str)]
    return []
