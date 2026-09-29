"""KB retrieval over pgvector with deterministic fallback behavior."""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from pydantic import BaseModel, ConfigDict, Field
from psycopg import sql

from src.kb.ingest import EmbeddingFunction, RuleChunk, default_embedding


class MappingCandidate(BaseModel):
    """Single mapping result row returned to the mapping stage."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    mapped_construct: str
    confidence: float = Field(ge=0.0, le=1.0)
    source_service: str
    target_service: str | None = None


@dataclass(frozen=True)
class SearchRow:
    """Backend search row before confidence thresholding."""

    rule_id: str
    source_service: str
    target_service: str
    target_construct: str
    rule_confidence: float
    semantic_similarity: float


class SearchBackend(Protocol):
    """Protocol for search backends used by RuleRetriever."""

    def search(
        self,
        query_embedding: list[float],
        source_service: str,
        k: int,
    ) -> list[SearchRow]:
        """Return candidate rows ranked by semantic relevance."""


class PgVectorSearchBackend:
    """Postgres+pgvector implementation of vector similarity search."""

    def __init__(self, dsn: str, table_name: str = "kb_rule_chunks") -> None:
        self._dsn = dsn
        self._table_name = table_name
        _validate_table_name(table_name)

    def search(
        self,
        query_embedding: list[float],
        source_service: str,
        k: int,
    ) -> list[SearchRow]:
        if k <= 0:
            raise ValueError("k must be a positive integer")

        query_vector = Vector(query_embedding)
        limit = max(k * 3, k)

        with psycopg.connect(self._dsn) as conn:
            register_vector(conn)
            with conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        """
                        SELECT
                            rule_id,
                            source_service,
                            target_service,
                            target_construct,
                            rule_confidence,
                            GREATEST(0.0, 1.0 - (embedding <=> %s)) AS semantic_similarity
                        FROM {table_name}
                        WHERE lower(source_service) = lower(%s)
                        ORDER BY embedding <=> %s
                        LIMIT %s
                        """
                    ).format(table_name=sql.Identifier(self._table_name)),
                    (query_vector, source_service, query_vector, limit),
                )
                rows = cur.fetchall()

        return [
            SearchRow(
                rule_id=str(row[0]),
                source_service=str(row[1]),
                target_service=str(row[2]),
                target_construct=str(row[3]),
                rule_confidence=float(row[4]),
                semantic_similarity=float(row[5]),
            )
            for row in rows
        ]


class InMemorySearchBackend:
    """Simple backend used for tests and local deterministic execution."""

    def __init__(self, chunks: Sequence[RuleChunk]) -> None:
        self._chunks = list(chunks)

    def search(
        self,
        query_embedding: list[float],
        source_service: str,
        k: int,
    ) -> list[SearchRow]:
        if k <= 0:
            raise ValueError("k must be a positive integer")

        candidates: list[SearchRow] = []
        for chunk in self._chunks:
            if chunk.source_service.lower() != source_service.lower():
                continue
            similarity = _cosine_similarity(query_embedding, chunk.embedding)
            candidates.append(
                SearchRow(
                    rule_id=chunk.rule_id,
                    source_service=chunk.source_service,
                    target_service=chunk.target_service,
                    target_construct=chunk.target_construct,
                    rule_confidence=chunk.rule_confidence,
                    semantic_similarity=similarity,
                )
            )

        candidates.sort(key=lambda row: row.semantic_similarity, reverse=True)
        return candidates[: max(k * 3, k)]


class RuleRetriever:
    """Retrieves mapping candidates and enforces confidence threshold semantics."""

    def __init__(
        self,
        backend: SearchBackend,
        embedding_function: EmbeddingFunction = default_embedding,
        min_confidence: float = 0.55,
    ) -> None:
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be between 0.0 and 1.0")

        self._backend = backend
        self._embedding_function = embedding_function
        self._min_confidence = min_confidence

    def retrieve(self, query: str, source_service: str, k: int) -> list[MappingCandidate]:
        if not query.strip():
            raise ValueError("query must not be empty")
        if not source_service.strip():
            raise ValueError("source_service must not be empty")
        if k <= 0:
            raise ValueError("k must be a positive integer")

        query_embedding = self._embedding_function(query)
        rows = self._backend.search(query_embedding=query_embedding, source_service=source_service, k=k)

        filtered: list[MappingCandidate] = []
        for row in rows:
            confidence = _combined_confidence(row.semantic_similarity, row.rule_confidence)
            if confidence < self._min_confidence:
                continue

            filtered.append(
                MappingCandidate(
                    rule_id=row.rule_id,
                    mapped_construct=row.target_construct,
                    confidence=confidence,
                    source_service=row.source_service,
                    target_service=row.target_service,
                )
            )
            if len(filtered) >= k:
                break

        if not filtered:
            return [
                MappingCandidate(
                    rule_id="UNMAPPED",
                    mapped_construct="UNMAPPED",
                    confidence=0.0,
                    source_service=source_service,
                    target_service=None,
                )
            ]

        return filtered


_DEFAULT_RETRIEVER: RuleRetriever | None = None


def configure_default_retriever(
    retriever: RuleRetriever | None = None,
    *,
    dsn: str | None = None,
    table_name: str = "kb_rule_chunks",
    min_confidence: float | None = None,
    embedding_function: EmbeddingFunction = default_embedding,
) -> RuleRetriever:
    """Configure or replace module-level default retriever instance."""

    global _DEFAULT_RETRIEVER

    if retriever is not None:
        _DEFAULT_RETRIEVER = retriever
        return retriever

    resolved_dsn = dsn or os.getenv(
        "KB_PGVECTOR_DSN", "postgresql://postgres:postgres@localhost:5432/aztoawz"
    )
    resolved_threshold = (
        min_confidence
        if min_confidence is not None
        else float(os.getenv("KB_MIN_CONFIDENCE", "0.55"))
    )

    backend = PgVectorSearchBackend(dsn=resolved_dsn, table_name=table_name)
    _DEFAULT_RETRIEVER = RuleRetriever(
        backend=backend,
        embedding_function=embedding_function,
        min_confidence=resolved_threshold,
    )
    return _DEFAULT_RETRIEVER


def get_default_retriever() -> RuleRetriever:
    """Return default retriever, creating one from environment settings if needed."""

    global _DEFAULT_RETRIEVER
    if _DEFAULT_RETRIEVER is None:
        _DEFAULT_RETRIEVER = configure_default_retriever()
    return _DEFAULT_RETRIEVER


def retrieve(query: str, source_service: str, k: int) -> list[MappingCandidate]:
    """Retrieve top-k mapped constructs from the KB or return UNMAPPED."""

    return get_default_retriever().retrieve(query=query, source_service=source_service, k=k)


def _combined_confidence(semantic_similarity: float, rule_confidence: float) -> float:
    semantic = max(0.0, min(1.0, semantic_similarity))
    prior = max(0.0, min(1.0, rule_confidence))
    return semantic * prior


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError(
            "Embedding dimension mismatch between query and indexed rules: "
            f"{len(left)} != {len(right)}"
        )

    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0

    dot = sum(l_value * r_value for l_value, r_value in zip(left, right, strict=True))
    return max(0.0, min(1.0, dot / (left_norm * right_norm)))


def _validate_table_name(table_name: str) -> None:
    if not table_name or not table_name.replace("_", "").isalnum() or table_name[0].isdigit():
        raise ValueError(f"Unsafe table name: {table_name}")
