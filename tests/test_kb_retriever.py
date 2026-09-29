from __future__ import annotations

from pathlib import Path

from src.kb.ingest import build_rule_chunks, load_kb_rules
from src.kb.retriever import InMemorySearchBackend, RuleRetriever


def _fixture_embedding(text: str) -> list[float]:
    lowered = text.lower()

    if "keyvault" in lowered or "key vault" in lowered or "secret" in lowered:
        return [1.0, 0.0, 0.0, 0.0]
    if "http" in lowered or "route" in lowered or "api" in lowered:
        return [0.0, 1.0, 0.0, 0.0]
    if "queue" in lowered:
        return [0.0, 0.0, 1.0, 0.0]
    return [0.0, 0.0, 0.0, 1.0]


def _write_fixture_rules(rules_dir: Path) -> None:
    rules_dir.mkdir(parents=True, exist_ok=True)

    (rules_dir / "kv-001-vault.yaml").write_text(
        """
rule_id: KV-001
source_service: Azure Key Vault
source_construct: Microsoft.KeyVault/vaults
target_service: AWS Secrets Stack
target_construct: AWS::SecretsManager::Secret + AWS::KMS::Key
property_map:
  vaultName: secretNamePrefix
caveats:
  - Map data-plane access with IAM separately.
examples:
  - source:
      name: kv-prod
    target:
      secret_prefix: kv-prod/
confidence: 0.95
last_verified: 2026-09-29
""".strip(),
        encoding="utf-8",
    )

    (rules_dir / "fn-003-http.yaml").write_text(
        """
rule_id: FN-003
source_service: Azure Functions
source_construct: HTTP trigger
target_service: AWS Lambda + API Gateway
target_construct: AWS::ApiGatewayV2::Api + Lambda integration
property_map:
  route: RouteKey
caveats:
  - Authorization semantics differ and require policy review.
examples:
  - source:
      route: /health
    target:
      RouteKey: GET /health
confidence: 0.90
last_verified: 2026-09-29
""".strip(),
        encoding="utf-8",
    )


def test_retrieve_returns_known_rule_for_matching_query(tmp_path: Path) -> None:
    rules_dir = tmp_path / "rules"
    _write_fixture_rules(rules_dir)

    rules = load_kb_rules(rules_dir)
    chunks = build_rule_chunks(rules, embedding_function=_fixture_embedding)
    backend = InMemorySearchBackend(chunks)
    retriever = RuleRetriever(backend=backend, embedding_function=_fixture_embedding, min_confidence=0.50)

    result = retriever.retrieve(
        query="Migrate key vault secrets to AWS with kms key",
        source_service="Azure Key Vault",
        k=1,
    )

    assert result[0].rule_id == "KV-001"
    assert result[0].mapped_construct == "AWS::SecretsManager::Secret + AWS::KMS::Key"
    assert result[0].confidence >= 0.50


def test_retrieve_returns_unmapped_when_no_rule_clears_threshold(tmp_path: Path) -> None:
    rules_dir = tmp_path / "rules"
    _write_fixture_rules(rules_dir)

    rules = load_kb_rules(rules_dir)
    chunks = build_rule_chunks(rules, embedding_function=_fixture_embedding)
    backend = InMemorySearchBackend(chunks)
    retriever = RuleRetriever(backend=backend, embedding_function=_fixture_embedding, min_confidence=0.70)

    result = retriever.retrieve(
        query="unknown construct with no documented mapping",
        source_service="Azure Key Vault",
        k=2,
    )

    assert len(result) == 1
    assert result[0].rule_id == "UNMAPPED"
    assert result[0].mapped_construct == "UNMAPPED"
    assert result[0].confidence == 0.0
