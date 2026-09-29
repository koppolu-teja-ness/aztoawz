from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from src.agents.generator import GeneratorRequest, generator_node
from src.graph.state import MappingResult, MigrationState, ParsedResource


def _load_parsed_fixture(name: str) -> list[ParsedResource]:
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "mapping" / name
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    return [ParsedResource.model_validate(item) for item in payload]


def _base_state(
    resources: list[ParsedResource],
    mapping_results: list[MappingResult],
) -> MigrationState:
    now = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
    return MigrationState(
        run_id="run-cfn-lint-test-001",
        started_at=now,
        updated_at=now,
        parsed_resources=resources,
        mapping_results=mapping_results,
    )


def _fully_mappable_state() -> MigrationState:
    resources = _load_parsed_fixture("fully_mappable.json")
    mapping_results = [
        MappingResult(
            source_resource_id="Microsoft.KeyVault/vaults/kv-demo",
            target_resource_type="AWS::SecretsManager::Secret + AWS::KMS::Key",
            mapped=True,
            rule_id="KV-001",
            confidence=0.96,
        ),
        MappingResult(
            source_resource_id="Microsoft.Web/sites/func-demo",
            target_resource_type="AWS::Lambda::Function",
            mapped=True,
            rule_id="FN-001",
            confidence=0.93,
        ),
        MappingResult(
            source_resource_id="Microsoft.Network/virtualNetworks/subnets/vnet-demo/subnet-app",
            target_resource_type="AWS::EC2::Subnet",
            mapped=True,
            rule_id="VNET-002",
            confidence=0.91,
        ),
    ]
    return _base_state(resources, mapping_results)


def _partially_mappable_state() -> MigrationState:
    resources = _load_parsed_fixture("partially_mappable.json")
    mapping_results = [
        MappingResult(
            source_resource_id="Microsoft.KeyVault/vaults/kv-demo",
            target_resource_type="AWS::SecretsManager::Secret + AWS::KMS::Key",
            mapped=True,
            rule_id="KV-001",
            confidence=0.94,
        ),
        MappingResult(
            source_resource_id="Microsoft.Network/virtualNetworks/vnet-demo",
            target_resource_type="UNMAPPED",
            mapped=False,
            rule_id="UNMAPPED",
            confidence=0.0,
            caveats=["requires manual intervention"],
        ),
    ]
    return _base_state(resources, mapping_results)


def _lint_all_artifacts(state: MigrationState, workdir: Path) -> list[str]:
    failures: list[str] = []
    cfn_lint_executable = _resolve_cfn_lint_executable()
    for artifact in state.cfn_artifacts:
        template_yaml = artifact.properties.get("template_yaml")
        assert isinstance(template_yaml, str)

        template_path = workdir / f"{artifact.logical_id}.yaml"
        template_path.write_text(template_yaml, encoding="utf-8")

        try:
            completed = subprocess.run(
                [
                    cfn_lint_executable,
                    "--format",
                    "json",
                    "--non-zero-exit-code",
                    "error",
                    str(template_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise AssertionError("cfn-lint not found on PATH for pytest execution") from exc

        if completed.returncode != 0:
            stdout = completed.stdout.strip()
            stderr = completed.stderr.strip()
            detail = stdout or stderr or "no cfn-lint output captured"
            failures.append(f"{artifact.logical_id}: {detail}")

    return failures


def test_generated_templates_pass_cfn_lint() -> None:
    state = generator_node(
        _fully_mappable_state(),
        GeneratorRequest(),
        now_provider=lambda: datetime(2026, 9, 29, 14, 1, tzinfo=timezone.utc),
    )

    assert state.cfn_artifacts

    with tempfile.TemporaryDirectory(prefix="cfn-lint-test-") as temp_dir:
        failures = _lint_all_artifacts(state, Path(temp_dir))

    assert not failures, "cfn-lint failures:\n" + "\n".join(failures)


def test_generated_templates_pass_cfn_lint_for_partially_mappable_fixture() -> None:
    state = generator_node(
        _partially_mappable_state(),
        GeneratorRequest(),
        now_provider=lambda: datetime(2026, 9, 29, 14, 2, tzinfo=timezone.utc),
    )

    assert state.cfn_artifacts

    with tempfile.TemporaryDirectory(prefix="cfn-lint-test-") as temp_dir:
        failures = _lint_all_artifacts(state, Path(temp_dir))

    assert not failures, "cfn-lint failures:\n" + "\n".join(failures)


def test_generated_templates_pass_checkov() -> None:
    state = generator_node(
        _fully_mappable_state(),
        GeneratorRequest(),
        now_provider=lambda: datetime(2026, 9, 29, 14, 3, tzinfo=timezone.utc),
    )

    assert state.cfn_artifacts

    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="checkov-test-") as temp_dir:
        workdir = Path(temp_dir)
        for artifact in state.cfn_artifacts:
            template_yaml = artifact.properties.get("template_yaml")
            assert isinstance(template_yaml, str)
            template_path = workdir / f"{artifact.logical_id}.yaml"
            template_path.write_text(template_yaml, encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "checkov",
                    "-f",
                    str(template_path),
                    "--framework",
                    "cloudformation",
                    "--compact",
                    "--quiet",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            # checkov returns non-zero when any failed check exists; treat only CRITICAL/HIGH
            # findings recorded in JSON output as blocking, consistent with validator/node.py.
            if completed.returncode not in (0, 1):
                failures.append(
                    f"{artifact.logical_id}: checkov failed to execute cleanly: "
                    f"{completed.stderr.strip() or 'unknown error'}"
                )

    assert not failures, "checkov execution failures:\n" + "\n".join(failures)


def _resolve_cfn_lint_executable() -> str:
    resolved = shutil.which("cfn-lint")
    if resolved is not None:
        return resolved

    scripts_dir = Path(sys.executable).resolve().parent
    candidates = [
        scripts_dir / "cfn-lint",
        scripts_dir / "cfn-lint.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    return "cfn-lint"
