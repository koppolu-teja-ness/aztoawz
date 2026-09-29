"""Ingest knowledge base rules into a pgvector-backed index."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import yaml
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg import sql

from src.kb.schema import KnowledgeBaseRule

EmbeddingFunction = Callable[[str], list[float]]
SUPPORTED_RULE_EXTENSIONS = {".yaml", ".yml", ".json"}


@dataclass(frozen=True)
class RuleChunk:
    """Single semantically self-contained rule chunk used for retrieval."""

    doc_id: str
    version_id: str
    rule_id: str
    source_service: str
    source_construct: str
    target_service: str
    target_construct: str
    rule_confidence: float
    chunk_text: str
    embedding: list[float]


def default_embedding(text: str, dimensions: int = 64) -> list[float]:
    """Deterministic fallback embedding for local development and tests."""

    vector = [0.0] * dimensions
    tokens = [token for token in text.lower().replace("\n", " ").split(" ") if token]
    if not tokens:
        return vector

    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], byteorder="little") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign

    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0:
        return vector
    return [component / norm for component in vector]


def load_kb_rules(rules_dir: str | Path) -> list[KnowledgeBaseRule]:
    """Load and validate all rule files in the provided directory."""

    path = Path(rules_dir)
    if not path.exists():
        raise FileNotFoundError(f"Rules directory not found: {path}")

    rules: list[KnowledgeBaseRule] = []
    for rule_file in sorted(path.iterdir()):
        if not rule_file.is_file() or rule_file.suffix.lower() not in SUPPORTED_RULE_EXTENSIONS:
            continue
        payload = _load_rule_payload(rule_file)
        rules.append(KnowledgeBaseRule.model_validate(payload))

    if not rules:
        raise ValueError(f"No KB rule files found in {path}")

    return rules


def chunk_rule(rule: KnowledgeBaseRule) -> str:
    """Create one semantic chunk per mapping rule with co-located details."""

    examples_json = json.dumps([example.model_dump(mode="json") for example in rule.examples], sort_keys=True)
    property_map_json = json.dumps(rule.property_map, sort_keys=True)
    caveats_text = "\n".join(f"- {caveat}" for caveat in rule.caveats) if rule.caveats else "- none"

    return "\n".join(
        [
            f"rule_id: {rule.rule_id}",
            f"source_service: {rule.source_service}",
            f"source_construct: {rule.source_construct}",
            f"target_service: {rule.target_service}",
            f"target_construct: {rule.target_construct}",
            f"confidence: {rule.confidence:.6f}",
            f"last_verified: {rule.last_verified.isoformat()}",
            f"property_map: {property_map_json}",
            "caveats:",
            caveats_text,
            f"examples: {examples_json}",
        ]
    )


def build_rule_chunks(
    rules: Iterable[KnowledgeBaseRule],
    embedding_function: EmbeddingFunction,
) -> list[RuleChunk]:
    """Build semantically indexed chunks from validated rules."""

    chunks: list[RuleChunk] = []
    for rule in rules:
        chunk_text = chunk_rule(rule)
        embedding = embedding_function(chunk_text)
        if not embedding:
            raise ValueError(f"Embedding function returned an empty vector for rule {rule.rule_id}")

        version_id = _version_id(rule, chunk_text)
        chunks.append(
            RuleChunk(
                doc_id=rule.rule_id,
                version_id=version_id,
                rule_id=rule.rule_id,
                source_service=rule.source_service,
                source_construct=rule.source_construct,
                target_service=rule.target_service,
                target_construct=rule.target_construct,
                rule_confidence=rule.confidence,
                chunk_text=chunk_text,
                embedding=embedding,
            )
        )

    _validate_embedding_dimensions(chunks)
    return chunks


def ingest_rules_to_pgvector(
    dsn: str,
    rules_dir: str | Path,
    embedding_function: EmbeddingFunction = default_embedding,
    table_name: str = "kb_rule_chunks",
) -> int:
    """Load, chunk, embed, and upsert KB rules into Postgres+pgvector."""

    _validate_table_name(table_name)
    rules = load_kb_rules(rules_dir)
    chunks = build_rule_chunks(rules, embedding_function)
    embedding_dim = len(chunks[0].embedding)

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            _ensure_pgvector_schema(cur, table_name, embedding_dim)
            register_vector(conn)
            for chunk in chunks:
                _upsert_chunk(cur, table_name, chunk)
        conn.commit()

    return len(chunks)


def _load_rule_payload(rule_file: Path) -> dict[str, Any]:
    with rule_file.open("r", encoding="utf-8") as file_handle:
        if rule_file.suffix.lower() == ".json":
            payload = json.load(file_handle)
        else:
            payload = yaml.safe_load(file_handle)

    if not isinstance(payload, dict):
        raise ValueError(f"Rule file must deserialize to a mapping: {rule_file}")

    return payload


def _version_id(rule: KnowledgeBaseRule, chunk_text: str) -> str:
    fingerprint = "|".join([rule.rule_id, rule.last_verified.isoformat(), chunk_text])
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def _validate_embedding_dimensions(chunks: list[RuleChunk]) -> None:
    dims = {len(chunk.embedding) for chunk in chunks}
    if len(dims) != 1:
        raise ValueError(f"Embedding function returned inconsistent dimensions: {sorted(dims)}")


def _validate_table_name(table_name: str) -> None:
    if not table_name or not table_name.replace("_", "").isalnum() or table_name[0].isdigit():
        raise ValueError(f"Unsafe table name: {table_name}")


def _ensure_pgvector_schema(cur: psycopg.Cursor[Any], table_name: str, embedding_dim: int) -> None:
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")

    cur.execute(
        sql.SQL(
            """
            CREATE TABLE IF NOT EXISTS {table_name} (
                id BIGSERIAL PRIMARY KEY,
                doc_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                source_service TEXT NOT NULL,
                source_construct TEXT NOT NULL,
                target_service TEXT NOT NULL,
                target_construct TEXT NOT NULL,
                rule_confidence DOUBLE PRECISION NOT NULL,
                chunk_text TEXT NOT NULL,
                embedding {embedding_type} NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (rule_id, version_id)
            )
            """
        ).format(
            table_name=sql.Identifier(table_name),
            embedding_type=sql.SQL(f"vector({embedding_dim})"),
        )
    )

    cur.execute(
        """
        SELECT atttypmod
        FROM pg_attribute
        WHERE attrelid = %s::regclass
          AND attname = 'embedding'
          AND NOT attisdropped
        """,
        (table_name,),
    )
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"Embedding column was not created for table {table_name}")

    atttypmod = int(row[0])
    # Different pgvector/driver combinations can expose either raw dim (e.g. 64)
    # or varlena typmod encoding (dim + 4). Accept either representation.
    if atttypmod > 0:
        valid_dims = {atttypmod, atttypmod - 4}
        if embedding_dim not in valid_dims:
            stored_dim = atttypmod - 4 if (atttypmod - 4) > 0 else atttypmod
            raise ValueError(
                f"Table {table_name} expects vector({stored_dim}), but embeddings are vector({embedding_dim})"
            )


def _upsert_chunk(cur: psycopg.Cursor[Any], table_name: str, chunk: RuleChunk) -> None:
    cur.execute(
        sql.SQL(
            """
            INSERT INTO {table_name} (
                doc_id,
                version_id,
                rule_id,
                source_service,
                source_construct,
                target_service,
                target_construct,
                rule_confidence,
                chunk_text,
                embedding
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
            ON CONFLICT (rule_id, version_id)
            DO UPDATE SET
                doc_id = EXCLUDED.doc_id,
                source_service = EXCLUDED.source_service,
                source_construct = EXCLUDED.source_construct,
                target_service = EXCLUDED.target_service,
                target_construct = EXCLUDED.target_construct,
                rule_confidence = EXCLUDED.rule_confidence,
                chunk_text = EXCLUDED.chunk_text,
                embedding = EXCLUDED.embedding
            """
        ).format(table_name=sql.Identifier(table_name)),
        (
            chunk.doc_id,
            chunk.version_id,
            chunk.rule_id,
            chunk.source_service,
            chunk.source_construct,
            chunk.target_service,
            chunk.target_construct,
            chunk.rule_confidence,
            chunk.chunk_text,
            _to_vector_literal(chunk.embedding),
        ),
    )


def _to_vector_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.12g}" for value in values) + "]"
