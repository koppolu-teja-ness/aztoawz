"""LangGraph builder for full migration pipeline orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, Literal, Protocol, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from src.agents.approval import ApprovalGateRequest, FileApprovalStore, approval_allows_deployment, approval_gate_node
from src.agents.deployer import DeploymentRequest, deployer_node
from src.agents.discovery import DiscoveryRequest, discovery_node
from src.agents.generator import GeneratorRequest, generator_node
from src.agents.mapping import MappingRequest, mapping_node
from src.agents.parser import ParserRequest, parser_analyzer_node
from src.agents.planner import PlannerRequest, planner_node
from src.agents.postvalidate import PostValidateRequest, postvalidate_node
from src.agents.reporter import ReporterRequest, reporter_node
from src.agents.validator import ValidatorRequest, validator_node
from src.graph.state import MigrationState


TraceBackend = Literal["none", "langsmith", "langfuse", "both", "custom"]
CheckpointBackend = Literal["sqlite", "postgres", "memory"]

_REDACTION_TOKEN = "***REDACTED***"
_SENSITIVE_KEY_PATTERN = re.compile(
	r"(?i)(secret|token|password|passphrase|apikey|api_key|clientsecret|privatekey|connectionstring|sas|certificate|auth)",
)
_SENSITIVE_VALUE_PATTERNS = [
	re.compile(r"AKIA[0-9A-Z]{16}"),
	re.compile(r"ASIA[0-9A-Z]{16}"),
	re.compile(r"(?i)-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
	re.compile(r"(?i)(secret|token|password|key)\s*[:=]\s*[^\s;]+"),
]


class GraphEnvelope(TypedDict):
	"""LangGraph mutable envelope for migration state transitions."""

	migration_state: MigrationState


class TraceSink(Protocol):
	"""Sink interface for trace events."""

	def emit(self, payload: dict[str, Any]) -> None:
		"""Emit one trace payload."""


@dataclass(slots=True)
class GraphDependencies:
	"""Optional dependency injection points for deterministic testing."""

	parser_command_runner: Callable[[list[str]], None] | None = None
	mapping_retriever: Callable[[str, str, int], list[Any]] | None = None
	validator_command_runner: Callable[[list[str], Path], Any] | None = None
	approval_store: FileApprovalStore | None = None
	deployer_cloudformation_client: Any | None = None
	postvalidate_boto3_session: Any | None = None


class CompositeTraceSink:
	"""Fan-out sink that writes events to all configured downstream sinks."""

	def __init__(self, sinks: list[TraceSink]) -> None:
		self._sinks = sinks

	def emit(self, payload: dict[str, Any]) -> None:
		for sink in self._sinks:
			try:
				sink.emit(payload)
			except Exception:
				# Tracing failures must not block deterministic migration execution.
				continue


class LangSmithTraceSink:
	"""Best-effort LangSmith sink."""

	def __init__(self, client: Any) -> None:
		self._client = client

	def emit(self, payload: dict[str, Any]) -> None:
		self._client.create_run(
			name="migration-graph-stage",
			run_type="chain",
			inputs=payload,
			tags=["migration", "langgraph"],
		)


class LangFuseTraceSink:
	"""Best-effort LangFuse sink."""

	def __init__(self, client: Any) -> None:
		self._client = client

	def emit(self, payload: dict[str, Any]) -> None:
		trace = self._client.trace(name="migration-graph-stage", input=payload)
		if hasattr(trace, "update"):
			trace.update(output={"status": "recorded"})


def build_migration_graph(
	*,
	discovery_request: DiscoveryRequest,
	parser_request: ParserRequest,
	mapping_request: MappingRequest | None = None,
	generator_request: GeneratorRequest | None = None,
	validator_request: ValidatorRequest | None = None,
	planner_request: PlannerRequest | None = None,
	approval_request: ApprovalGateRequest | None = None,
	deployment_request: DeploymentRequest | None = None,
	postvalidate_request: PostValidateRequest | None = None,
	reporter_request: ReporterRequest | None = None,
	checkpointer: Any | None = None,
	checkpoint_backend: CheckpointBackend = "sqlite",
	checkpoint_connection: str | None = None,
	trace_backend: TraceBackend = "none",
	trace_sinks: list[TraceSink] | None = None,
	dependencies: GraphDependencies | None = None,
) -> Any:
	"""Build and compile the full migration pipeline graph with approval interrupt support."""

	mapping_request = mapping_request or MappingRequest()
	generator_request = generator_request or GeneratorRequest()
	validator_request = validator_request or ValidatorRequest()
	planner_request = planner_request or PlannerRequest()
	approval_request = approval_request or ApprovalGateRequest()
	deployment_request = deployment_request or DeploymentRequest()
	postvalidate_request = postvalidate_request or PostValidateRequest()
	reporter_request = reporter_request or ReporterRequest()
	dependencies = dependencies or GraphDependencies()

	sink = _build_trace_sink(trace_backend=trace_backend, explicit_sinks=trace_sinks)

	def _with_trace(stage: str, state: MigrationState, request: Any, execute: Callable[[], MigrationState]) -> MigrationState:
		_emit_trace(
			sink,
			{
				"event": "stage_start",
				"stage": stage,
				"run_id": state.run_id,
				"request": _model_dump_safe(request),
				"state": state.model_dump(mode="json"),
			},
		)
		try:
			updated = execute()
		except Exception as exc:
			_emit_trace(
				sink,
				{
					"event": "stage_error",
					"stage": stage,
					"run_id": state.run_id,
					"error_type": type(exc).__name__,
					"error_message": str(exc),
				},
			)
			raise

		_emit_trace(
			sink,
			{
				"event": "stage_end",
				"stage": stage,
				"run_id": state.run_id,
				"state": updated.model_dump(mode="json"),
				"audit_record_count": len(updated.audit_records),
			},
		)
		return updated

	def discover(envelope: GraphEnvelope) -> dict[str, MigrationState]:
		current = envelope["migration_state"]
		updated = _with_trace(
			"discover",
			current,
			discovery_request,
			lambda: discovery_node(current, discovery_request),
		)
		return {"migration_state": updated}

	def parse(envelope: GraphEnvelope) -> dict[str, MigrationState]:
		current = envelope["migration_state"]
		updated = _with_trace(
			"parse",
			current,
			parser_request,
			lambda: parser_analyzer_node(
				current,
				parser_request,
				command_runner=dependencies.parser_command_runner,
			),
		)
		return {"migration_state": updated}

	def map_stage(envelope: GraphEnvelope) -> dict[str, MigrationState]:
		current = envelope["migration_state"]
		updated = _with_trace(
			"map",
			current,
			mapping_request,
			lambda: mapping_node(
				current,
				mapping_request,
				retriever=dependencies.mapping_retriever,
			),
		)
		return {"migration_state": updated}

	def generate(envelope: GraphEnvelope) -> dict[str, MigrationState]:
		current = envelope["migration_state"]
		updated = _with_trace(
			"generate",
			current,
			generator_request,
			lambda: generator_node(current, generator_request),
		)
		return {"migration_state": updated}

	def validate_static(envelope: GraphEnvelope) -> dict[str, MigrationState]:
		current = envelope["migration_state"]
		updated = _with_trace(
			"validate_static",
			current,
			validator_request,
			lambda: validator_node(
				current,
				validator_request,
				command_runner=dependencies.validator_command_runner,
			),
		)
		return {"migration_state": updated}

	def plan_score(envelope: GraphEnvelope) -> dict[str, MigrationState]:
		current = envelope["migration_state"]
		updated = _with_trace(
			"plan_score",
			current,
			planner_request,
			lambda: planner_node(current, planner_request),
		)
		return {"migration_state": updated}

	def approval_gate(envelope: GraphEnvelope) -> dict[str, MigrationState]:
		current = envelope["migration_state"]
		updated = _with_trace(
			"approval",
			current,
			approval_request,
			lambda: approval_gate_node(
				current,
				approval_request,
				store=dependencies.approval_store,
			),
		)
		return {"migration_state": updated}

	def deploy(envelope: GraphEnvelope) -> dict[str, MigrationState]:
		current = envelope["migration_state"]
		updated = _with_trace(
			"deploy",
			current,
			deployment_request,
			lambda: deployer_node(
				current,
				deployment_request,
				cloudformation_client=dependencies.deployer_cloudformation_client,
			),
		)
		return {"migration_state": updated}

	def validate_post(envelope: GraphEnvelope) -> dict[str, MigrationState]:
		current = envelope["migration_state"]
		updated = _with_trace(
			"validate_post",
			current,
			postvalidate_request,
			lambda: postvalidate_node(
				current,
				postvalidate_request,
				boto3_session=dependencies.postvalidate_boto3_session,
			),
		)
		return {"migration_state": updated}

	def report(envelope: GraphEnvelope) -> dict[str, MigrationState]:
		current = envelope["migration_state"]
		updated = _with_trace(
			"report",
			current,
			reporter_request,
			lambda: reporter_node(current, reporter_request),
		)
		return {"migration_state": updated}

	def route_after_approval(envelope: GraphEnvelope) -> str:
		return "deploy" if approval_allows_deployment(envelope["migration_state"]) else "report"

	graph = StateGraph(GraphEnvelope)
	graph.add_node("discover", discover)
	graph.add_node("parse", parse)
	graph.add_node("map", map_stage)
	graph.add_node("generate", generate)
	graph.add_node("validate_static", validate_static)
	graph.add_node("plan_score", plan_score)
	graph.add_node("approval", approval_gate)
	graph.add_node("deploy", deploy)
	graph.add_node("validate_post", validate_post)
	graph.add_node("report", report)

	graph.add_edge(START, "discover")
	graph.add_edge("discover", "parse")
	graph.add_edge("parse", "map")
	graph.add_edge("map", "generate")
	graph.add_edge("generate", "validate_static")
	graph.add_edge("validate_static", "plan_score")
	graph.add_edge("plan_score", "approval")
	graph.add_conditional_edges(
		"approval",
		route_after_approval,
		{
			"deploy": "deploy",
			"report": "report",
		},
	)
	graph.add_edge("deploy", "validate_post")
	graph.add_edge("validate_post", "report")
	graph.add_edge("report", END)

	compiled = graph.compile(
		checkpointer=checkpointer
		or build_graph_checkpointer(
			backend=checkpoint_backend,
			connection=checkpoint_connection,
		)
	)
	return compiled


def build_graph_checkpointer(
	*,
	backend: CheckpointBackend,
	connection: str | None = None,
) -> Any:
	"""Construct a checkpointer backend for interrupt/resume durability."""

	if backend == "memory":
		return MemorySaver()

	if backend == "sqlite":
		sqlite_conn = connection or os.getenv(
			"MIGRATION_GRAPH_CHECKPOINT",
			".checkpoints/migration_graph.sqlite",
		)
		Path(sqlite_conn).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

		import sqlite3

		from langgraph.checkpoint.sqlite import SqliteSaver

		# SqliteSaver.from_conn_string() is a contextmanager-decorated generator whose
		# saver is closed on exit; build the saver from a long-lived raw connection
		# instead so it stays usable across separate invoke()/resume() calls.
		raw_connection = sqlite3.connect(sqlite_conn, check_same_thread=False)
		return SqliteSaver(raw_connection)

	if backend == "postgres":
		postgres_conn = connection or os.getenv("MIGRATION_GRAPH_POSTGRES_DSN")
		if not postgres_conn:
			raise ValueError("Postgres checkpointer requires connection string")

		from langgraph.checkpoint.postgres import PostgresSaver

		return PostgresSaver.from_conn_string(postgres_conn)

	raise ValueError(f"Unsupported checkpointer backend: {backend}")


def redact_trace_payload(payload: Any) -> Any:
	"""Recursively redact sensitive fields before trace emission."""

	if isinstance(payload, dict):
		redacted: dict[str, Any] = {}
		for key, value in payload.items():
			if _SENSITIVE_KEY_PATTERN.search(str(key)):
				redacted[str(key)] = _REDACTION_TOKEN
			else:
				redacted[str(key)] = redact_trace_payload(value)
		return redacted

	if isinstance(payload, list):
		return [redact_trace_payload(item) for item in payload]

	if isinstance(payload, tuple):
		return tuple(redact_trace_payload(item) for item in payload)

	if isinstance(payload, str):
		for pattern in _SENSITIVE_VALUE_PATTERNS:
			if pattern.search(payload):
				return _REDACTION_TOKEN
		return payload

	return payload


def _build_trace_sink(*, trace_backend: TraceBackend, explicit_sinks: list[TraceSink] | None) -> TraceSink | None:
	if trace_backend == "none" and not explicit_sinks:
		return None

	sinks: list[TraceSink] = list(explicit_sinks or [])

	if trace_backend in {"langsmith", "both"}:
		try:
			from langsmith import Client

			sinks.append(LangSmithTraceSink(Client()))
		except Exception:
			pass

	if trace_backend in {"langfuse", "both"}:
		try:
			from langfuse import Langfuse

			sinks.append(LangFuseTraceSink(Langfuse()))
		except Exception:
			pass

	if not sinks:
		return None
	return CompositeTraceSink(sinks)


def _emit_trace(sink: TraceSink | None, payload: dict[str, Any]) -> None:
	if sink is None:
		return
	sink.emit(redact_trace_payload(payload))


def _model_dump_safe(model: Any) -> dict[str, Any]:
	if hasattr(model, "model_dump"):
		dumped = model.model_dump(mode="json")
		if isinstance(dumped, dict):
			return dumped
	return {}

