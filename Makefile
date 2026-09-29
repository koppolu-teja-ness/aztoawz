.PHONY: install lint format typecheck test validate-cfn validate-kb scan run-graph run-api run-ui

BICEP_PATH ?= tests/fixtures/bicep/functionapp_sample.bicep
RUN_DIR ?= .migration_runs/manual

install:
	python -m pip install --upgrade pip
	python -m pip install -e .[dev]

lint:
	ruff check .
	black --check .

format:
	black .
	ruff check . --fix

typecheck:
	mypy src tests

test:
	pytest -q

validate-cfn:
	cfn-lint infra/cfn/*.yaml infra/cfn/*.yml infra/cfn/*.cfn.yaml

validate-kb:
	python scripts/validate_kb.py

scan:
	checkov -d infra/cfn
	$(MAKE) validate-kb

run-graph:
	python -m src.graph.cli start --bicep-path "$(BICEP_PATH)" --run-dir "$(RUN_DIR)"

run-api:
	uvicorn src.api:create_app --factory --host 0.0.0.0 --port 8000 --reload

run-ui:
	streamlit run src/ui/app.py
