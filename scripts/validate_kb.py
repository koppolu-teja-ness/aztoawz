"""Validate knowledge-base rule files against schema and uniqueness constraints."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.kb.schema import KnowledgeBaseRule

LOGGER = logging.getLogger("validate_kb")
RULES_DIR = ROOT_DIR / "kb" / "rules"
SUPPORTED_EXTENSIONS = {".yaml", ".yml", ".json"}


def _load_rule_payload(rule_file: Path) -> dict[str, Any]:
    with rule_file.open("r", encoding="utf-8") as file_handle:
        if rule_file.suffix == ".json":
            payload = json.load(file_handle)
        else:
            payload = yaml.safe_load(file_handle)

    if not isinstance(payload, dict):
        raise ValueError("Rule file must deserialize to an object/mapping")

    return payload


def validate_rules_directory(rules_dir: Path) -> int:
    if not rules_dir.exists():
        LOGGER.error("Rules directory does not exist: %s", rules_dir)
        return 1

    rule_files = sorted(
        file_path
        for file_path in rules_dir.iterdir()
        if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not rule_files:
        LOGGER.error("No rule files found in %s", rules_dir)
        return 1

    errors: list[str] = []
    seen_rule_ids: dict[str, Path] = {}

    for rule_file in rule_files:
        try:
            payload = _load_rule_payload(rule_file)
            rule = KnowledgeBaseRule.model_validate(payload)
        except (json.JSONDecodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
            errors.append(f"{rule_file}: {exc}")
            continue

        prior = seen_rule_ids.get(rule.rule_id)
        if prior is not None:
            errors.append(
                f"Duplicate rule_id '{rule.rule_id}' found in {rule_file} and {prior}"
            )
        else:
            seen_rule_ids[rule.rule_id] = rule_file

    if errors:
        for error in errors:
            LOGGER.error(error)
        LOGGER.error(
            "KB validation failed: %d file(s) checked, %d error(s)",
            len(rule_files),
            len(errors),
        )
        return 1

    LOGGER.info("KB validation passed: %d file(s), %d unique rule IDs", len(rule_files), len(seen_rule_ids))
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    return validate_rules_directory(RULES_DIR)


if __name__ == "__main__":
    sys.exit(main())
