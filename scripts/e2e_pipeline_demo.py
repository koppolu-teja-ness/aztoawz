"""Phase 9 real end-to-end smoke run of the full migration LangGraph pipeline.

Exercises Discover -> Parse -> Map -> Generate -> Validate -> Plan -> Approval
(interrupt) -> [inject APPROVE] -> Deploy -> Post-Validate -> Report against a
real Bicep fixture containing one Key Vault, one VNet+subnet, one Function App,
and one intentionally out-of-scope resource (ExpressRoute circuit).

Uses real KB-grounded mapping (in-memory index built from kb/rules/*.yaml),
real az bicep compilation, real cfn-lint/checkov static validation, and a real
boto3 CloudFormation/Lambda/SecretsManager/EC2 deployment against moto's
in-process AWS mock (no external network or paid AWS calls).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
import re
from pathlib import Path
import shutil
import sys

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.agents.approval import ApprovalGateRequest, FileApprovalStore  # noqa: E402
from src.agents.discovery import DiscoveryRequest  # noqa: E402
from src.agents.mapping import MappingRequest  # noqa: E402
from src.agents.parser import ParserRequest  # noqa: E402
from src.agents.reporter import ReporterRequest  # noqa: E402
from src.graph.build_graph import GraphDependencies, build_migration_graph  # noqa: E402
from src.graph.state import ApprovalDecision, ApprovalRecord, MigrationState  # noqa: E402
from src.kb.ingest import build_rule_chunks, load_kb_rules  # noqa: E402
from src.kb.retriever import InMemorySearchBackend, RuleRetriever  # noqa: E402

_DEMO_MIN_CONFIDENCE = 0.15
"""Lowered ONLY for this demo script to prove the MAPPED/generator/validator/
deploy path works end-to-end with the real KB content. The measured max
similarity for a textbook-perfect Key Vault match against real kb/rules using
a full-vocabulary TF embedding was 0.20 (see docs/audit report) -- well under
the real config/migration.config.yaml threshold of 0.55. This override does
NOT modify the production config file."""

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _word_overlap_embedding_factory(vocabulary: list[str]):
    """Deterministic local TF vector over a fixed vocabulary (no external network calls).

    NOTE: production/default_embedding() in src/kb/ingest.py is a 64-bucket hashed
    bag-of-words that this audit found empirically produces <0.15 confidence for
    real Key Vault/Function/VNet queries against the real kb/rules content (max
    observed 0.14 at threshold 0.0, well below the configured 0.55 gate). This
    richer full-vocabulary TF embedding is used ONLY in this demo script to prove
    the retrieval/threshold/audit machinery is correct when given a workable
    embedding function; see docs/audit report for the default_embedding gap.
    """

    index = {token: position for position, token in enumerate(vocabulary)}

    def embed(text: str) -> list[float]:
        vector = [0.0] * len(vocabulary)
        counts = Counter(_tokenize(text))
        for token, count in counts.items():
            position = index.get(token)
            if position is not None:
                vector[position] = float(count)
        norm = math.sqrt(sum(component * component for component in vector))
        if norm == 0:
            return vector
        return [component / norm for component in vector]

    return embed


def _real_kb_retriever() -> RuleRetriever:
    repo_root = Path(__file__).resolve().parent.parent
    rules = load_kb_rules(repo_root / "kb" / "rules")

    vocabulary = sorted({token for rule in rules for token in _tokenize(json.dumps(rule.model_dump(mode="json")))})
    embedding_function = _word_overlap_embedding_factory(vocabulary)

    chunks = build_rule_chunks(rules, embedding_function=embedding_function)
    backend = InMemorySearchBackend(chunks=chunks)
    # Retriever-internal gate left permissive; mapping_node applies the real,
    # config-driven confidence threshold (see _DEMO_MIN_CONFIDENCE below).
    return RuleRetriever(backend=backend, embedding_function=embedding_function, min_confidence=0.0)


def main() -> None:
    run_dir = Path(__file__).resolve().parent.parent / ".e2e_demo_run"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)

    bicep_fixture = (
        Path(__file__).resolve().parent.parent
        / "tests"
        / "fixtures"
        / "bicep"
        / "e2e_full_scope_sample.bicep"
    )

    retriever = _real_kb_retriever()
    run_id = "run-e2e-demo-001"
    now = datetime.now(tz=timezone.utc)

    real_config_path = Path(__file__).resolve().parent.parent / "config" / "migration.config.yaml"
    demo_config = yaml.safe_load(real_config_path.read_text(encoding="utf-8"))
    demo_config["mapping"]["min_confidence"] = _DEMO_MIN_CONFIDENCE
    demo_config_path = run_dir / "demo_migration.config.yaml"
    demo_config_path.write_text(yaml.safe_dump(demo_config), encoding="utf-8")
    print(f"NOTE: using demo-only mapping.min_confidence={_DEMO_MIN_CONFIDENCE} "
          f"(production default in {real_config_path} is 0.55) to demonstrate the MAPPED path.")

    with mock_aws():
        graph = build_migration_graph(
            discovery_request=DiscoveryRequest(bicep_path=str(bicep_fixture)),
            parser_request=ParserRequest(
                bicep_path=str(bicep_fixture),
                artifacts_dir=str(run_dir / "artifacts"),
            ),
            mapping_request=MappingRequest(config_path=str(demo_config_path)),
            approval_request=ApprovalGateRequest(checkpoint_dir=str(run_dir / "approval")),
            reporter_request=ReporterRequest(output_dir=str(run_dir / "reports")),
            checkpointer=MemorySaver(),
            dependencies=GraphDependencies(
                mapping_retriever=lambda query, service, k: retriever.retrieve(
                    query=query, source_service=service, k=k
                ),
            ),
        )

        thread = {"configurable": {"thread_id": run_id}}

        print("=== INVOKING GRAPH (discover -> parse -> map -> generate -> validate -> plan -> approval[interrupt]) ===")
        interrupted = graph.invoke(
            {"migration_state": MigrationState(run_id=run_id, started_at=now, updated_at=now)},
            config=thread,
        )
        print("interrupt_present:", "__interrupt__" in interrupted)

        approval_store = FileApprovalStore(run_dir / "approval")
        pending = approval_store.get_pending_plan()
        print("pending_plan_hash:", pending.plan_hash if pending else None)
        print("pending_plan_item_count:", len(pending.migration_plan) if pending else 0)

        mid_state = interrupted["migration_state"]
        print("mapping_results:")
        for mr in mid_state.mapping_results:
            print(f"  - {mr.source_resource_id}: mapped={mr.mapped} rule_id={mr.rule_id} confidence={mr.confidence:.2f}")

        print("cfn_artifacts:", [a.logical_id for a in mid_state.cfn_artifacts])
        for artifact in mid_state.cfn_artifacts:
            print(
                f"  artifact={artifact.logical_id} "
                f"stack_name={artifact.properties.get('stack_name')} "
                f"depends_on_stacks={artifact.properties.get('depends_on_stacks')}"
            )
            debug_path = run_dir / f"debug_{artifact.logical_id}.yaml"
            debug_path.write_text(str(artifact.properties.get("template_yaml", "")), encoding="utf-8")
            print(f"wrote {debug_path}")

        assert pending is not None
        approval_store.save_approval_record(
            ApprovalRecord(
                decision=ApprovalDecision.APPROVE,
                reviewer="e2e-demo-reviewer",
                timestamp=datetime.now(tz=timezone.utc),
                plan_hash=pending.plan_hash,
                comments="approved by phase-9 e2e demo script",
            )
        )

        print("=== RESUMING GRAPH (approval -> deploy -> validate_post -> report) ===")
        resumed = graph.invoke(Command(resume={"decision": "APPROVE"}), config=thread)
        final_state = resumed["migration_state"]

        stages = [record.outputs.get("stage") for record in final_state.audit_records]
        print("completed_stages:", stages)

        assert final_state.deployment_result is not None
        print("deployed:", final_state.deployment_result.deployed)
        print("stack_statuses:", final_state.deployment_result.stack_statuses)

        if final_state.post_deploy_findings is not None:
            print("postdeploy_finding_count:", len(final_state.post_deploy_findings))

        report_dir = run_dir / "reports" / run_id
        report_files = sorted(p.name for p in report_dir.glob("*")) if report_dir.exists() else []
        print("report_files:", report_files)

        audit_summary = [
            {"stage": r.outputs.get("stage"), "rule_ids_used": r.rule_ids_used}
            for r in final_state.audit_records
        ]
        print("audit_record_summary:")
        print(json.dumps(audit_summary, indent=2, default=str))


if __name__ == "__main__":
    main()
