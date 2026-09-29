"""Mapping agent node for RAG-grounded Azure-to-AWS construct mapping."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.graph.state import AuditRecord, MappingResult, MigrationState, ParsedResource
from src.kb.retriever import MappingCandidate, retrieve
from src.tools.config import load_migration_config


class MappingRequest(BaseModel):
    """Input contract for mapping stage execution."""

    model_config = ConfigDict(extra="forbid")

    config_path: str | None = None
    candidates_per_resource: int | None = Field(default=None, ge=1, le=20)


RetrieverCallable = Callable[[str, str, int], list[MappingCandidate]]


def mapping_node(
    state: MigrationState,
    request: MappingRequest,
    *,
    retriever: RetrieverCallable | None = None,
    now_provider: Callable[[], datetime] | None = None,
) -> MigrationState:
    """Run mapping over parsed resources and append mapping-stage audit metadata."""

    config = load_migration_config(request.config_path)
    min_confidence = config.mapping.min_confidence
    top_k = request.candidates_per_resource or config.mapping.candidates_per_resource

    do_retrieve = retriever or retrieve
    now_fn = now_provider or (lambda: datetime.now(tz=timezone.utc))

    mapping_results: list[MappingResult] = []
    rule_ids_used: list[str] = []
    manual_items: list[str] = []

    for resource in state.parsed_resources:
        source_service = _resolve_source_service(resource)
        if source_service is None:
            mapping_results.append(
                _unmapped_result(
                    resource=resource,
                    caveat="UNMAPPED: no supported mapping service classification for source type",
                )
            )
            manual_items.append(
                "UNMAPPED requires manual intervention: "
                f"{resource.resource_id} ({resource.source_type})"
            )
            continue

        query = _build_query(resource)
        candidates = do_retrieve(query, source_service, top_k)

        if _is_intentionally_unsupported(resource):
            mapping_results.append(
                _unmapped_result(
                    resource=resource,
                    caveat=(
                        "UNMAPPED: ExpressRoute is out-of-scope for auto-migration per "
                        "azure-vnet-to-vpc mapping table"
                    ),
                )
            )
            manual_items.append(
                "UNMAPPED requires manual intervention: "
                f"{resource.resource_id} ({resource.source_type})"
            )
            continue

        selected = _select_candidate(candidates, min_confidence)

        if selected is None:
            mapping_results.append(
                _unmapped_result(
                    resource=resource,
                    caveat=(
                        "UNMAPPED: no mapping rule met confidence threshold "
                        f"{min_confidence:.2f}"
                    ),
                )
            )
            manual_items.append(
                "UNMAPPED requires manual intervention: "
                f"{resource.resource_id} ({resource.source_type})"
            )
            continue

        mapping_results.append(
            MappingResult(
                source_resource_id=resource.resource_id,
                target_resource_type=selected.mapped_construct,
                mapped=True,
                rule_id=selected.rule_id,
                confidence=selected.confidence,
                caveats=[],
            )
        )
        rule_ids_used.append(selected.rule_id)

    timestamp = now_fn()
    merged_manual_dependencies = _merge_unique(state.manual_dependencies, manual_items)
    unique_rule_ids = sorted(set(rule_ids_used))

    audit = AuditRecord(
        inputs_hash=_stable_hash(
            {
                "run_id": state.run_id,
                "parsed_resources": [
                    item.model_dump(mode="json") for item in state.parsed_resources
                ],
                "request": request.model_dump(mode="json"),
                "min_confidence": min_confidence,
                "top_k": top_k,
            }
        ),
        outputs={
            "stage": "mapping",
            "parsed_count": len(state.parsed_resources),
            "mapped_count": sum(1 for item in mapping_results if item.mapped),
            "unmapped_count": sum(1 for item in mapping_results if not item.mapped),
            "manual_dependency_count": len(merged_manual_dependencies),
            "confidence_threshold": min_confidence,
            "candidates_per_resource": top_k,
        },
        rule_ids_used=unique_rule_ids,
        reviewer=None,
        timestamp=timestamp,
    )

    return state.model_copy(
        update={
            "updated_at": timestamp,
            "mapping_results": mapping_results,
            "manual_dependencies": merged_manual_dependencies,
            "audit_records": [*state.audit_records, audit],
        }
    )


def _resolve_source_service(resource: ParsedResource) -> str | None:
    source_type = resource.source_type.lower()

    if source_type.startswith("microsoft.keyvault/"):
        return "Azure Key Vault"
    if source_type.startswith("microsoft.web/sites"):
        return "Azure Functions"
    if source_type.startswith("microsoft.network/"):
        return "Azure Virtual Network"
    if source_type.startswith("microsoft.authorization/"):
        return "Azure RBAC"
    return None


def _is_intentionally_unsupported(resource: ParsedResource) -> bool:
    source_type = resource.source_type.lower()
    return source_type.startswith("microsoft.network/expressroute")


def _build_query(resource: ParsedResource) -> str:
    properties_payload = json.dumps(resource.properties, sort_keys=True, default=str)
    return "\n".join(
        [
            f"resource_id: {resource.resource_id}",
            f"source_type: {resource.source_type}",
            f"normalized_type: {resource.normalized_type}",
            f"properties: {properties_payload}",
        ]
    )


def _select_candidate(
    candidates: list[MappingCandidate],
    min_confidence: float,
) -> MappingCandidate | None:
    best: MappingCandidate | None = None

    for candidate in candidates:
        if candidate.rule_id == "UNMAPPED":
            continue
        if candidate.confidence < min_confidence:
            continue
        if best is None or candidate.confidence > best.confidence:
            best = candidate

    return best


def _unmapped_result(resource: ParsedResource, caveat: str) -> MappingResult:
    return MappingResult(
        source_resource_id=resource.resource_id,
        target_resource_type="UNMAPPED",
        mapped=False,
        rule_id="UNMAPPED",
        confidence=0.0,
        caveats=[caveat, "requires manual intervention"],
    )


def _merge_unique(existing: list[str], incoming: list[str]) -> list[str]:
    merged = list(existing)
    seen = set(existing)
    for item in incoming:
        if item in seen:
            continue
        merged.append(item)
        seen.add(item)
    return merged


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
