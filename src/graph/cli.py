"""Real CLI entrypoint for running the migration LangGraph pipeline outside the API/tests."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from langgraph.types import Command

from src.agents.approval import ApprovalGateRequest, FileApprovalStore
from src.agents.discovery import DiscoveryRequest
from src.agents.parser import ParserRequest
from src.agents.reporter import ReporterRequest
from src.graph.build_graph import build_migration_graph
from src.graph.state import ApprovalDecision, ApprovalRecord, MigrationState

_RUN_CONFIG_FILE = "run_config.json"


def _run_config_path(run_dir: Path) -> Path:
    return run_dir / _RUN_CONFIG_FILE


def _build_graph(run_dir: Path, run_config: dict[str, object]) -> object:
    return build_migration_graph(
        discovery_request=DiscoveryRequest(
            bicep_path=run_config.get("bicep_path"),
            use_live_azure=bool(run_config.get("use_live_azure", False)),
            subscription_id=run_config.get("subscription_id"),
            resource_group=run_config.get("resource_group"),
        ),
        parser_request=ParserRequest(
            bicep_path=str(run_config["bicep_path"]),
            artifacts_dir=str(run_dir / "artifacts"),
        ),
        approval_request=ApprovalGateRequest(checkpoint_dir=str(run_dir / "approval")),
        reporter_request=ReporterRequest(output_dir=str(run_dir / "reports")),
        checkpoint_backend="sqlite",
        checkpoint_connection=str(run_dir / "graph.sqlite"),
    )


def _cmd_start(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    run_config: dict[str, object] = {
        "bicep_path": args.bicep_path,
        "use_live_azure": args.use_live_azure,
        "subscription_id": args.subscription_id,
        "resource_group": args.resource_group,
    }
    _run_config_path(run_dir).write_text(json.dumps(run_config, indent=2), encoding="utf-8")

    run_id = args.run_id or f"run-{run_dir.name}"
    graph = _build_graph(run_dir, run_config)
    thread = {"configurable": {"thread_id": run_id}}
    now = datetime.now(tz=timezone.utc)

    result = graph.invoke(  # type: ignore[attr-defined]
        {"migration_state": MigrationState(run_id=run_id, started_at=now, updated_at=now)},
        config=thread,
    )

    state: MigrationState = result["migration_state"]
    print(f"run_id={run_id}")
    print(f"plan_hash={state.plan_hash}")
    print(f"pending_approval_path={run_dir / 'approval' / 'pending_plan.json'}")
    print("Awaiting human approval. Run 'python -m src.graph.cli resume ...' after recording a decision.")


def _cmd_resume(args: argparse.Namespace) -> None:
    run_dir = Path(args.run_dir)
    run_config = json.loads(_run_config_path(run_dir).read_text(encoding="utf-8"))

    approval_store = FileApprovalStore(run_dir / "approval")
    pending = approval_store.get_pending_plan()
    if pending is None:
        raise SystemExit("No pending approval plan found; run 'start' first.")

    now = datetime.now(tz=timezone.utc)
    approval_store.save_approval_record(
        ApprovalRecord(
            decision=ApprovalDecision(args.decision),
            reviewer=args.reviewer,
            timestamp=now,
            plan_hash=pending.plan_hash,
            comments=args.comments,
        )
    )

    graph = _build_graph(run_dir, run_config)
    thread = {"configurable": {"thread_id": pending.run_id}}
    resumed = graph.invoke(Command(resume={"decision": args.decision}), config=thread)  # type: ignore[attr-defined]

    state: MigrationState = resumed["migration_state"]
    stages = [record.outputs.get("stage") for record in state.audit_records]
    print(f"completed_stages={stages}")
    if state.deployment_result is not None:
        print(f"deployed={state.deployment_result.deployed}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Azure-to-AWS migration LangGraph pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser(
        "start",
        help="Discover, parse, map, generate, validate, and plan a run up to human approval",
    )
    start_parser.add_argument("--bicep-path", required=True)
    start_parser.add_argument("--run-dir", required=True)
    start_parser.add_argument("--run-id", default=None)
    start_parser.add_argument("--use-live-azure", action="store_true")
    start_parser.add_argument("--subscription-id", default=None)
    start_parser.add_argument("--resource-group", default=None)
    start_parser.set_defaults(func=_cmd_start)

    resume_parser = subparsers.add_parser(
        "resume",
        help="Record a human decision and resume the run through deployment and reporting",
    )
    resume_parser.add_argument("--run-dir", required=True)
    resume_parser.add_argument("--decision", required=True, choices=["APPROVE", "REJECT", "MODIFY"])
    resume_parser.add_argument("--reviewer", required=True)
    resume_parser.add_argument("--comments", default=None)
    resume_parser.set_defaults(func=_cmd_resume)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
